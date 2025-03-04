# Copyright The Lightning AI team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import functools
import os
import threading
from contextlib import _GeneratorContextManager, contextmanager, nullcontext
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Generator, Iterable, List, Literal, Optional, Tuple, Type, TYPE_CHECKING, Union

import torch
from torch import Tensor
from torch.nn import Module
from torch.optim import Optimizer

from lightning.fabric.accelerators import Accelerator
from lightning.fabric.plugins import CheckpointIO, ClusterEnvironment, Precision
from lightning.fabric.plugins.collectives.torch_collective import default_pg_timeout
from lightning.fabric.plugins.precision.fsdp import FSDPPrecision
from lightning.fabric.strategies.launchers.subprocess_script import _SubprocessScriptLauncher
from lightning.fabric.strategies.parallel import ParallelStrategy
from lightning.fabric.strategies.registry import _StrategyRegistry
from lightning.fabric.strategies.strategy import (
    _apply_filter,
    _BackwardSyncControl,
    _Sharded,
    _validate_keys_for_strict_loading,
    TBroadcast,
)
from lightning.fabric.utilities.distributed import (
    _get_default_process_group_backend_for_device,
    _init_dist_connection,
    _sync_ddp_if_available,
)
from lightning.fabric.utilities.distributed import group as _group
from lightning.fabric.utilities.distributed import ReduceOp
from lightning.fabric.utilities.imports import (
    _TORCH_GREATER_EQUAL_1_12,
    _TORCH_GREATER_EQUAL_1_13,
    _TORCH_GREATER_EQUAL_2_0,
)
from lightning.fabric.utilities.init import _EmptyInit
from lightning.fabric.utilities.rank_zero import rank_zero_only, rank_zero_warn
from lightning.fabric.utilities.seed import reset_seed
from lightning.fabric.utilities.types import _PATH

_SUPPORTS_OPTIMIZER_IN_FSDP_BACKWARD = False
if _TORCH_GREATER_EQUAL_2_0 and torch.distributed.is_available():
    from torch.distributed.fsdp._common_utils import _get_module_fsdp_state
    from torch.distributed.fsdp._traversal_utils import _get_fsdp_handles
    from torch.distributed.fsdp.flat_param import FlatParameter, FlatParamHandle

    _SUPPORTS_OPTIMIZER_IN_FSDP_BACKWARD = True

if TYPE_CHECKING:
    from torch.distributed.fsdp.fully_sharded_data_parallel import CPUOffload, FullyShardedDataParallel, MixedPrecision

    from lightning.fabric.wrappers import _FabricModule

_FSDP_ALIASES = ("fsdp", "fsdp_cpu_offload")
_METADATA_FILENAME = "meta.pt"


class FSDPStrategy(ParallelStrategy, _Sharded):
    r"""Strategy for Fully Sharded Data Parallel provided by torch.distributed.

    .. warning::  This is an :ref:`experimental <versioning:Experimental API>` feature.

    Fully Sharded Training shards the entire model across all available GPUs, allowing you to scale model
    size, whilst using efficient communication to reduce overhead. In practice, this means we can remain
    at parity with PyTorch DDP, whilst scaling our model sizes dramatically. The technique is similar
    to ZeRO-Stage 3.

    For more information check out
    `this blogpost <https://pytorch.org/blog/introducing-pytorch-fully-sharded-data-parallel-api>`__.

    Defaults have been set and options have been exposed, but may require configuration
    based on your level of memory/speed efficiency. We suggest having a look at
    `this tutorial <https://pytorch.org/tutorials/intermediate/FSDP_tutorial.html>`__ for more information.

    Arguments:
        cpu_offload: See ``cpu_offload`` parameter in :class:`torch.distributed.fsdp.FullyShardedDataParallel`.
        mixed_precision: See ``mixed_precision`` parameter in :class:`torch.distributed.fsdp.FullyShardedDataParallel`.
        activation_checkpointing: A single layer or a list of layer classes for which you want to enable activation
            checkpointing. This is typically your transformer block (including attention + feed-forward).
            Enabling this can free up a significant amount of memory at the cost of speed since activations in
            these layers need to be recomputed during backpropagation.
        state_dict_type: The format in which the state of the model and optimizers gets saved into the checkpoint.

            - ``"full"``: The full weights and optimizer states get assembled on rank 0 and saved to a single file.
            - ``"sharded"``: Each rank saves its shard of weights and optimizer states to a file. The checkpoint is
              a folder with as many files as the world size.

        \**kwargs: See available parameters in :class:`torch.distributed.fsdp.FullyShardedDataParallel`.
    """

    def __init__(
        self,
        accelerator: Optional[Accelerator] = None,
        parallel_devices: Optional[List[torch.device]] = None,
        cluster_environment: Optional[ClusterEnvironment] = None,
        checkpoint_io: Optional[CheckpointIO] = None,
        precision: Optional[Precision] = None,
        process_group_backend: Optional[str] = None,
        timeout: Optional[timedelta] = default_pg_timeout,
        cpu_offload: Union[bool, "CPUOffload", None] = None,
        mixed_precision: Optional["MixedPrecision"] = None,
        activation_checkpointing: Optional[Union[Type[Module], List[Type[Module]]]] = None,
        state_dict_type: Literal["full", "sharded"] = "sharded",
        **kwargs: Any,
    ) -> None:
        if not _TORCH_GREATER_EQUAL_1_12:
            raise NotImplementedError("`FSDPStrategy` is supported from PyTorch v1.12.0 onwards.")

        super().__init__(
            accelerator=accelerator,
            parallel_devices=parallel_devices,
            cluster_environment=cluster_environment,
            checkpoint_io=checkpoint_io,
            precision=precision,
        )
        self._num_nodes = 1
        self._process_group_backend: Optional[str] = process_group_backend
        self._timeout: Optional[timedelta] = timeout
        self._backward_sync_control = _FSDPBackwardSyncControl()
        self._fsdp_kwargs = kwargs

        if _TORCH_GREATER_EQUAL_2_0:
            # Enables joint setup of model and optimizer, multiple optimizer param groups, and `torch.compile()`
            self._fsdp_kwargs.setdefault("use_orig_params", True)

        if activation_checkpointing and not _TORCH_GREATER_EQUAL_1_13:
            raise ValueError("Activation checkpointing requires torch >= 1.13.0. HINT: `pip install -U torch`")
        activation_checkpointing = activation_checkpointing or []
        self._activation_checkpointing = (
            [activation_checkpointing] if not isinstance(activation_checkpointing, list) else activation_checkpointing
        )
        self._state_dict_type = state_dict_type
        self.cpu_offload = _init_cpu_offload(cpu_offload)
        self.mixed_precision = mixed_precision

    @property
    def root_device(self) -> torch.device:
        assert self.parallel_devices is not None
        return self.parallel_devices[self.local_rank]

    @property
    def num_nodes(self) -> int:
        return self._num_nodes

    @num_nodes.setter
    def num_nodes(self, num_nodes: int) -> None:
        self._num_nodes = num_nodes

    @property
    def num_processes(self) -> int:
        return len(self.parallel_devices) if self.parallel_devices is not None else 0

    @property
    def distributed_sampler_kwargs(self) -> Dict[str, Any]:
        return {"num_replicas": (self.num_nodes * self.num_processes), "rank": self.global_rank}

    @property
    def process_group_backend(self) -> Optional[str]:
        return self._process_group_backend

    @property
    def mixed_precision_config(self) -> Optional["MixedPrecision"]:
        if self.mixed_precision:
            return self.mixed_precision
        if isinstance(self.precision, FSDPPrecision):
            return self.precision.mixed_precision_config
        return None

    def _configure_launcher(self) -> None:
        assert self.cluster_environment is not None
        if not self.cluster_environment.creates_processes_externally:
            self._launcher = _SubprocessScriptLauncher(self.cluster_environment, self.num_processes, self.num_nodes)

    def setup_environment(self) -> None:
        self._setup_distributed()
        super().setup_environment()

    def setup_module_and_optimizers(
        self, module: Module, optimizers: List[Optimizer]
    ) -> Tuple[Module, List[Optimizer]]:
        """Wraps the model into a
        :class:`~torch.distributed.fsdp.fully_sharded_data_parallel.FullyShardedDataParallel` module
        and sets `use_orig_params=True` to keep the reference to the original parameters in the
        optimizer.
        """
        if not _TORCH_GREATER_EQUAL_2_0:
            raise NotImplementedError(
                f"The `{type(self).__name__}` does not support the joint setup of module and optimizer(s)."
                " Please do it in this order: Create the model, call `setup_module`, create the optimizer,"
                " call `setup_optimizer`."
            )
        use_orig_params = self._fsdp_kwargs.get("use_orig_params")
        if use_orig_params is False:
            raise ValueError(
                f"You set `{type(self).__name__}(use_orig_params=False)` but this is not supported when"
                " setting the model and optimizer up jointly. Either set it to `True` or set the objects"
                " up in this order: Create the model, call `setup_module`, create the optimizer,"
                " call `setup_optimizer`."
            )
        module = self.setup_module(module)
        return module, optimizers

    def setup_module(self, module: Module) -> "FullyShardedDataParallel":
        """Wraps the model into a
        :class:`~torch.distributed.fsdp.fully_sharded_data_parallel.FullyShardedDataParallel` module."""
        from torch.distributed.fsdp.fully_sharded_data_parallel import FullyShardedDataParallel

        if "auto_wrap_policy" in self._fsdp_kwargs and any(
            isinstance(mod, FullyShardedDataParallel) for mod in module.modules()
        ):
            # If model is already wrapped, we need to avoid sending the `auto_wrap_policy`
            del self._fsdp_kwargs["auto_wrap_policy"]
        wrapped_module = FullyShardedDataParallel(
            module=module,
            cpu_offload=self.cpu_offload,
            mixed_precision=self.mixed_precision_config,
            device_id=self.root_device.index,
            **self._fsdp_kwargs,
        )

        # activation checkpointing needs to be set up after wrapping the model
        if _TORCH_GREATER_EQUAL_1_13 and self._activation_checkpointing:
            _setup_activation_checkpointing(module=wrapped_module, layers=self._activation_checkpointing)

        return wrapped_module

    def setup_optimizer(self, optimizer: Optimizer) -> Optimizer:
        """Set up an optimizer for a model wrapped with FSDP.

        This setup method doesn't modify the optimizer or wrap the optimizer. The only thing it currently does is verify
        that the optimizer was created after the model was wrapped with :meth:`setup_module` with a reference to the
        flattened parameters.
        """
        if _TORCH_GREATER_EQUAL_2_0:
            return optimizer

        from torch.distributed.fsdp import FlatParameter

        num_groups = len(optimizer.param_groups)
        if num_groups > 1:
            raise ValueError(
                "An optimizer used with an FSDP model does not support multiple param groups."
                f" Found {num_groups} parameter groups."
            )

        if any(isinstance(param, FlatParameter) for param in optimizer.param_groups[0]["params"]):
            return optimizer

        raise ValueError(
            "The optimizer does not seem to reference any FSDP parameters. HINT: Make sure to create the optimizer"
            " after setting up the model."
        )

    def module_to_device(self, module: Module) -> None:
        pass

    @contextmanager
    def module_init_context(self, empty_init: Optional[bool] = None) -> Generator[None, None, None]:
        # TODO: Use the meta device and reset parameters after https://github.com/pytorch/pytorch/issues/90465
        # is resolved. For now, the module will get moved to the device in `setup_module`.
        empty_init_context = _EmptyInit(enabled=bool(empty_init)) if _TORCH_GREATER_EQUAL_1_13 else nullcontext()
        with empty_init_context, self.precision.init_context(), self.module_sharded_context():
            yield

    @contextmanager
    def module_sharded_context(self) -> Generator:
        from torch.distributed.fsdp.fully_sharded_data_parallel import FullyShardedDataParallel
        from torch.distributed.fsdp.wrap import enable_wrap

        with enable_wrap(
            wrapper_cls=FullyShardedDataParallel,
            cpu_offload=self.cpu_offload,
            mixed_precision=self.mixed_precision_config,
            device_id=self.root_device.index,
            **self._fsdp_kwargs,
        ):
            yield

    def all_reduce(
        self, tensor: Tensor, group: Optional[Any] = None, reduce_op: Optional[Union[ReduceOp, str]] = "mean"
    ) -> Tensor:
        if isinstance(tensor, Tensor):
            return _sync_ddp_if_available(tensor, group, reduce_op=reduce_op)
        return tensor

    def barrier(self, *args: Any, **kwargs: Any) -> None:
        if not torch.distributed.is_initialized():
            return
        if torch.distributed.get_backend() == "nccl":
            torch.distributed.barrier(device_ids=[self.root_device.index])
        else:
            torch.distributed.barrier()

    def broadcast(self, obj: TBroadcast, src: int = 0) -> TBroadcast:
        if not torch.distributed.is_initialized():
            return obj

        obj = [obj]
        torch.distributed.broadcast_object_list(obj, src, group=_group.WORLD)
        return obj[0]

    def clip_gradients_norm(  # type: ignore[override]
        self,
        module: "FullyShardedDataParallel",
        optimizer: Optimizer,
        max_norm: Union[float, int],
        norm_type: Union[float, int] = 2.0,
        error_if_nonfinite: bool = True,
    ) -> Tensor:
        """Clip gradients by norm."""
        rank_zero_warn("Gradient Clipping by Norm is currently experimental for FSDP. Proceed with Caution!")
        self.precision.unscale_gradients(optimizer)
        return module.clip_grad_norm_(max_norm=max_norm, norm_type=norm_type)

    def clip_gradients_value(  # type: ignore[override]
        self, module: "FullyShardedDataParallel", optimizer: Optimizer, clip_val: Union[float, int]
    ) -> None:
        """Clip gradients by value."""

        raise NotImplementedError(
            "FSDP currently does not support to clip gradients by value. "
            "Consider clipping by norm instead or choose another strategy!"
        )

    def save_checkpoint(
        self,
        path: _PATH,
        state: Dict[str, Union[Module, Optimizer, Any]],
        storage_options: Optional[Any] = None,
        filter: Optional[Dict[str, Callable[[str, Any], bool]]] = None,
    ) -> None:
        """Save model, optimizer, and other state to a checkpoint on disk.

        If the state-dict-type is ``'full'``, the checkpoint will be written to a single file containing the weights,
        optimizer state and other metadata. If the state-dict-type is ``'sharded'``, the checkpoint gets saved as a
        directory containing one file per process, with model- and optimizer shards stored per file. Additionally, it
        creates a metadata file `meta.pt` with the rest of the user's state (only saved from rank 0).
        """
        if not _TORCH_GREATER_EQUAL_2_0:
            raise NotImplementedError(
                "Saving and loading checkpoints with the `FSDPStrategy` is not supported in PyTorch < 2.0."
                " Please upgrade `torch` or file an issue: `https://github.com/Lightning-AI/lightning/issues`."
            )
        if storage_options is not None:
            raise TypeError(
                "`FSDPStrategy.save_checkpoint(..., storage_options=...)` is not supported because"
                " `FSDPStrategy` does not use the `CheckpointIO`."
            )
        # broadcast the path from rank 0 to ensure all the states are saved in a common path
        path = Path(self.broadcast(path))
        if path.is_dir() and os.listdir(path):
            raise FileExistsError(f"The checkpoint directory already exists and is not empty: {path}")

        from torch.distributed.checkpoint import FileSystemWriter, save_state_dict
        from torch.distributed.fsdp import FullyShardedDataParallel as FSDP

        modules = [module for module in state.values() if isinstance(module, FSDP)]
        if len(modules) == 0:
            raise ValueError(
                "Could not find a FSDP model in the provided checkpoint state. Please provide the model as"
                " part of the state like so: `save_checkpoint(..., state={'model': model, ...})`. Make sure"
                " you set up the model (and optimizers if any) through the strategy before saving the checkpoint."
            )
        if len(modules) > 1:
            raise ValueError(
                "Found multiple FSDP modules in the given state. Saving checkpoints with FSDP is"
                " currently limited to a single model per checkpoint. To save multiple models, call the"
                " save method for each model separately with a different path."
            )

        module = modules[0]

        if self._state_dict_type == "sharded":
            path.mkdir(parents=True, exist_ok=True)
            state_dict_ctx = _get_sharded_state_dict_context(module)

            # replace the modules and optimizer objects in the state with their local state dict
            # and separate the user's metadata
            converted_state: Dict[str, Any] = {}
            metadata: Dict[str, Any] = {}
            with state_dict_ctx:
                for key, obj in state.items():
                    converted: Any
                    if isinstance(obj, FSDP):
                        converted = obj.state_dict()
                        target_dict = converted_state
                    elif isinstance(obj, Optimizer):
                        converted = FSDP.optim_state_dict(module, obj)
                        target_dict = converted_state
                    else:  # everything not a module or optimizer is considered metadata
                        converted = obj
                        target_dict = metadata
                    _apply_filter(key, filter or {}, converted, target_dict)

            # FSDP's FileSystemWriter streams the tensors to disk to minimize memory peaks
            writer = FileSystemWriter(path=path, single_file_per_rank=True)
            save_state_dict(converted_state, writer)

            if self.global_rank == 0:
                torch.save(metadata, path / _METADATA_FILENAME)

        elif self._state_dict_type == "full":
            state_dict_ctx = _get_full_state_dict_context(module)
            full_state: Dict[str, Any] = {}
            with state_dict_ctx:
                for key, obj in state.items():
                    if isinstance(obj, FSDP):
                        converted = obj.state_dict()
                    elif isinstance(obj, Optimizer):
                        converted = FSDP.optim_state_dict(module, obj)
                    else:  # everything not a module or optimizer is considered metadata
                        converted = obj
                    _apply_filter(key, filter or {}, converted, full_state)

            if self.global_rank == 0:
                torch.save(full_state, path)
        else:
            raise ValueError(f"Unknown state_dict_type: {self._state_dict_type}")

    def load_checkpoint(
        self,
        path: _PATH,
        state: Optional[Dict[str, Union[Module, Optimizer, Any]]] = None,
        strict: bool = True,
    ) -> Dict[str, Any]:
        """Load the contents from a checkpoint and restore the state of the given objects.

        The strategy currently only supports saving and loading sharded checkpoints which are stored in form of a
        directory of multiple files rather than a single file.
        """
        if not _TORCH_GREATER_EQUAL_2_0:
            raise NotImplementedError(
                "Saving and loading checkpoints with the `FSDPStrategy` is not supported in PyTorch < 2.0."
                " Please upgrade `torch` or file an issue: `https://github.com/Lightning-AI/lightning/issues`."
            )
        if not state:
            raise ValueError(
                f"Got FSDPStrategy.load_checkpoint(..., state={state!r}) but a state with at least "
                f" a model instance to reload is required. Pass it in like so:"
                " FSDPStrategy.load_checkpoint(..., state={'model': model, ...})"
            )
        # broadcast the path from rank 0 to ensure all the states are loaded from a common path
        path = Path(self.broadcast(path))

        from torch.distributed.checkpoint import FileSystemReader, load_state_dict
        from torch.distributed.checkpoint.optimizer import load_sharded_optimizer_state_dict
        from torch.distributed.fsdp import FullyShardedDataParallel as FSDP
        from torch.distributed.fsdp import OptimStateKeyType

        modules = {key: module for key, module in state.items() if isinstance(module, FSDP)}
        optimizers = {key: optim for key, optim in state.items() if isinstance(optim, Optimizer)}
        if len(modules) == 0:
            raise ValueError(
                "Could not find a FSDP model in the provided checkpoint state. Please provide the model as"
                " part of the state like so: `load_checkpoint(..., state={'model': model, ...})`. Make sure"
                " you set up the model (and optimizers if any) through the strategy before loading the checkpoint."
            )
        if len(modules) > 1:
            raise ValueError(
                "Found multiple FSDP modules in the given state. Loading checkpoints with FSDP is"
                " currently limited to a single model per checkpoint. To load multiple models, call the"
                " load method for each model separately with a different path."
            )
        module_key, module = list(modules.items())[0]

        if _is_sharded_checkpoint(path):
            state_dict_ctx = _get_sharded_state_dict_context(module)
            reader = FileSystemReader(path=path)

            with state_dict_ctx:
                module_state = {module_key: module.state_dict()}
                load_state_dict(module_state, reader)
                module.load_state_dict(module_state[module_key], strict=strict)

                # the optimizer states must be loaded separately
                for optim_key, optim in optimizers.items():
                    optim_state = load_sharded_optimizer_state_dict(
                        model_state_dict=module_state[module_key],
                        optimizer_key=optim_key,
                        storage_reader=reader,
                    )
                    flattened_osd = FSDP.optim_state_dict_to_load(
                        optim_state_dict=optim_state[optim_key],
                        model=module,
                        optim=optim,
                    )
                    optim.load_state_dict(flattened_osd)

            # Load metadata (anything not a module or optimizer)
            metadata = torch.load(path / _METADATA_FILENAME)
            requested_metadata_keys = state.keys() - modules.keys() - optimizers.keys()
            _validate_keys_for_strict_loading(requested_metadata_keys, metadata.keys(), strict=strict)
            for key in requested_metadata_keys:
                if key not in metadata:
                    continue
                state[key] = metadata.pop(key)

            # return the remaining metadata that wasn't requested as part of `state`
            return metadata

        if _is_full_checkpoint(path):
            # This is inefficient, as multiple copies of the checkpoint are held in CPU memory at once.
            # There is currently no other way because `summon_full_params` does not support write-back from rank 0 only.
            checkpoint = torch.load(path, map_location="cpu")
            with FSDP.summon_full_params(module, writeback=True, rank0_only=False):
                module.load_state_dict(checkpoint.pop(module_key), strict=strict)

            # Load optimizer states
            for optim_key, optim in optimizers.items():
                # rank0_only should be false because we need to load the optimizer state on all ranks
                with _get_full_state_dict_context(module, rank0_only=False):
                    temp_state_dict = checkpoint.pop(optim_key)

                    # Handling the case where the optimizer state is saved from a normal optimizer
                    if isinstance(list(temp_state_dict["state"].keys())[0], int):
                        temp_state_dict = FSDP.rekey_optim_state_dict(
                            temp_state_dict, OptimStateKeyType.PARAM_NAME, module
                        )

                    optim_state_dict = FSDP.optim_state_dict_to_load(
                        optim_state_dict=temp_state_dict,
                        model=module,
                        optim=optim,
                    )
                    optim.load_state_dict(optim_state_dict)

            requested_metadata_keys = state.keys() - modules.keys() - optimizers.keys()
            _validate_keys_for_strict_loading(requested_metadata_keys, checkpoint.keys(), strict=strict)

            # Load metadata (anything not a module or optimizer)
            for key in requested_metadata_keys:
                if key not in checkpoint:
                    continue
                state[key] = checkpoint.pop(key)

            # return the remaining metadata that wasn't requested as part of `state`
            return checkpoint

        raise ValueError(
            f"The path {str(path)!r} does not point to a valid checkpoint. Make sure the path points to either a"
            " directory with FSDP checkpoint shards, or a single file with a full checkpoint."
        )

    @classmethod
    def register_strategies(cls, strategy_registry: _StrategyRegistry) -> None:
        if not _TORCH_GREATER_EQUAL_1_12 or not torch.distributed.is_available():
            return

        strategy_registry.register(
            "fsdp",
            cls,
            description="Fully Sharded Data Parallel (FSDP) training",
        )
        strategy_registry.register(
            "fsdp_cpu_offload",
            cls,
            description="Fully Sharded Data Parallel (FSDP) training with Full Sharding and CPU Offloading",
            cpu_offload=True,
        )

    def _setup_distributed(self) -> None:
        reset_seed()
        self._set_world_ranks()
        self._process_group_backend = self._get_process_group_backend()
        assert self.cluster_environment is not None
        _init_dist_connection(self.cluster_environment, self._process_group_backend, timeout=self._timeout)

    def _get_process_group_backend(self) -> str:
        return self._process_group_backend or _get_default_process_group_backend_for_device(self.root_device)

    def _set_world_ranks(self) -> None:
        if self.cluster_environment is not None:
            self.cluster_environment.set_global_rank(self.node_rank * self.num_processes + self.local_rank)
            self.cluster_environment.set_world_size(self.num_nodes * self.num_processes)
        # `LightningEnvironment.set_global_rank` will do this too, but we cannot rely on that implementation detail
        # additionally, for some implementations, the setter is a no-op, so it's safer to access the getter
        rank_zero_only.rank = self.global_rank


def _setup_activation_checkpointing(module: "FullyShardedDataParallel", layers: List[Type[Module]]) -> None:
    from torch.distributed.algorithms._checkpoint.checkpoint_wrapper import (
        apply_activation_checkpointing,
        checkpoint_wrapper,
        CheckpointImpl,
    )

    check_fn = lambda submodule: isinstance(submodule, tuple(layers))
    wrapper = functools.partial(
        checkpoint_wrapper,
        checkpoint_impl=CheckpointImpl.NO_REENTRANT,
    )
    apply_activation_checkpointing(module, checkpoint_wrapper_fn=wrapper, check_fn=check_fn)


class _FSDPBackwardSyncControl(_BackwardSyncControl):
    @contextmanager
    def no_backward_sync(self, module: Module) -> Generator:
        """Blocks gradient synchronization inside the
        :class:`~torch.distributed.fsdp.FullyShardedDataParallel` wrapper."""
        from torch.distributed.fsdp.fully_sharded_data_parallel import FullyShardedDataParallel

        if not isinstance(module, FullyShardedDataParallel):
            raise TypeError(
                "Blocking backward sync is only possible if the module passed to"
                f" `{self.__class__.__name__}.no_backward_sync` is wrapped in `FullyShardedDataParallel`."
                f" Got: {module.__class__.__name__}."
            )
        with module.no_sync():
            yield


def _init_cpu_offload(cpu_offload: Optional[Union[bool, "CPUOffload"]]) -> "CPUOffload":
    from torch.distributed.fsdp import CPUOffload

    return cpu_offload if isinstance(cpu_offload, CPUOffload) else CPUOffload(offload_params=bool(cpu_offload))


def _optimizer_has_flat_params(optimizer: Optimizer) -> bool:
    _FSDP_FLATTENED = "_fsdp_flattened"
    if _TORCH_GREATER_EQUAL_1_13:
        return any(
            getattr(param, _FSDP_FLATTENED, False) for group in optimizer.param_group for param in group["params"]
        )

    from torch.distributed.fsdp import FlatParameter

    return any(isinstance(param, FlatParameter) for group in optimizer.param_groups for param in group["params"])


def _get_sharded_state_dict_context(module: "FullyShardedDataParallel") -> _GeneratorContextManager:
    from torch.distributed.fsdp import FullyShardedDataParallel as FSDP
    from torch.distributed.fsdp.api import ShardedOptimStateDictConfig, ShardedStateDictConfig, StateDictType

    state_dict_config = ShardedStateDictConfig(offload_to_cpu=True)
    optim_state_dict_config = ShardedOptimStateDictConfig(offload_to_cpu=True)
    state_dict_type_context = FSDP.state_dict_type(
        module=module,
        state_dict_type=StateDictType.SHARDED_STATE_DICT,
        state_dict_config=state_dict_config,
        optim_state_dict_config=optim_state_dict_config,
    )
    return state_dict_type_context


def _get_full_state_dict_context(
    module: "FullyShardedDataParallel", rank0_only: bool = True
) -> _GeneratorContextManager:
    from torch.distributed.fsdp import FullyShardedDataParallel as FSDP
    from torch.distributed.fsdp.api import FullOptimStateDictConfig, FullStateDictConfig, StateDictType

    state_dict_config = FullStateDictConfig(offload_to_cpu=True, rank0_only=rank0_only)
    optim_state_dict_config = FullOptimStateDictConfig(offload_to_cpu=True, rank0_only=rank0_only)
    state_dict_type_context = FSDP.state_dict_type(
        module=module,
        state_dict_type=StateDictType.FULL_STATE_DICT,
        state_dict_config=state_dict_config,
        optim_state_dict_config=optim_state_dict_config,
    )
    return state_dict_type_context


def _is_sharded_checkpoint(path: Path) -> bool:
    """A heuristic check to determine whether the path points to a directory with checkpoint shards."""
    return path.is_dir() and (path / _METADATA_FILENAME).is_file()


def _is_full_checkpoint(path: Path) -> bool:
    return path.is_file()


def _no_op() -> None:
    pass


@contextmanager
def _apply_optimizers_during_fsdp_backward(
    optimizers: Union[Optimizer, Iterable[Optimizer]],
    module: torch.nn.Module,
) -> Generator[None, None, None]:
    """Call `Optimizer.step` as gradients become available.

    NOTE: This is an EXPERIMENTAL utility and exploits behavior which is not
          part of the FSDP public API. Use at your own risk.

    By moving optimizer step invocation into the backward call we can free
    gradients earlier and reduce peak memory.
    """
    assert _SUPPORTS_OPTIMIZER_IN_FSDP_BACKWARD
    apply_lock = threading.Lock()

    param_handles = _get_fsdp_handles(module)
    assert param_handles, f"Module {module} does not appear to contain any FSDP modules."
    fsdp_state = _get_module_fsdp_state(module)
    assert fsdp_state is not None
    fsdp_stream = fsdp_state._streams["post_backward"]

    if isinstance(optimizers, Optimizer):
        optimizers = [optimizers]

    # We cannot trigger the optimizer step until all parameters are ready.
    remaining = {}
    for optimizer in optimizers:
        unfinished: Dict[torch.nn.Parameter, None] = {}  # Use Dict as an ordered set.
        for group in optimizer.param_groups:
            for p in group["params"]:
                if p not in unfinished:
                    assert p not in remaining, f"{p=} is shared between two optimizers."
                    unfinished[p] = None
                    remaining[p] = (optimizer, unfinished)

    def maybe_step(parameters: Iterable[torch.nn.Parameter], post_step: Callable[[], None] = _no_op) -> None:
        for p in tuple(parameters):
            optimizer, unfinished = remaining.pop(p)
            unfinished.pop(p)
            if not unfinished:
                optimizer.step()
                optimizer.zero_grad()

                # Used to call `_clear_grads_if_needed`. Otherwise FSDP might hold on to the memory.
                post_step()

    try:
        hook_handles = []
        for h in param_handles:
            assert isinstance(h, FlatParamHandle)
            flat_param = h.flat_param
            fsdp_acc_grad, _ = flat_param._post_backward_hook_state  # type: ignore

            # We must take `h` and `flat_param` as arguments because Python
            # late binds closures.
            def _opt_hook(h: FlatParamHandle, flat_param: FlatParameter, *_unused: Any) -> None:
                assert flat_param._post_backward_called
                assert h.flat_param is flat_param
                with apply_lock, torch.cuda.stream(fsdp_stream):
                    # We invoke `prepare_gradient_for_optim` earlier than usual.
                    # We also need to prevent the later "normal" invocation,
                    # otherwise the double call will trigger FSDP asserts.
                    prepare_gradient = h.prepare_gradient_for_optim
                    assert hasattr(prepare_gradient, "__func__"), prepare_gradient
                    assert prepare_gradient.__func__ is FlatParamHandle.prepare_gradient_for_optim
                    prepare_gradient()
                    h.prepare_gradient_for_optim = _no_op  # type: ignore[method-assign]
                    maybe_step(flat_param._params or (), h._clear_grads_if_needed)

            hook = functools.partial(_opt_hook, h, flat_param)
            hook_handles.append(fsdp_acc_grad.register_hook(hook))

        yield

    finally:
        # Non-FSDP parameters won't have a grad hook, so handle them here.
        with apply_lock:
            maybe_step(remaining)

        # Unregister the grad hooks.
        for hook_handle in hook_handles:
            hook_handle.remove()

        # And lastly back out the handle monkey patches.
        for h in param_handles:
            if h.prepare_gradient_for_optim is _no_op:
                del h.prepare_gradient_for_optim


def fsdp_overlap_step_with_backward(
    optimizers: Union[Optimizer, Iterable[Optimizer]],
    fabric_module: "_FabricModule",
) -> _GeneratorContextManager:
    from lightning.fabric.wrappers import _FabricModule

    assert isinstance(fabric_module, _FabricModule)
    return _apply_optimizers_during_fsdp_backward(optimizers, fabric_module._forward_module)
