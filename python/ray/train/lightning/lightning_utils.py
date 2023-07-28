import os
import ray
from ray.air import session
from ray.air.constants import MODEL_KEY
from ray.data.dataset import DataIterator
from ray.util import PublicAPI
from ray.train.lightning.lightning_checkpoint import LightningCheckpoint

import logging
import shutil
import torch
import tempfile
from packaging.version import Version
from typing import Any, Dict, Optional, List
from torch.utils.data import IterableDataset, DataLoader

import pytorch_lightning as pl
from pytorch_lightning.callbacks import ModelCheckpoint
from pytorch_lightning.plugins.environments import LightningEnvironment
from pytorch_lightning.strategies import DDPStrategy, DeepSpeedStrategy

if Version(pl.__version__) >= Version("2.0.0"):
    from pytorch_lightning.strategies import FSDPStrategy
else:
    from pytorch_lightning.strategies import DDPFullyShardedStrategy as FSDPStrategy


logger = logging.getLogger(__name__)

LIGHTNING_REPORT_STAGE_KEY = "_report_on"


def get_worker_root_device():
    """Get the first torch device of the current worker if there are multiple."""
    devices = ray.train.torch.get_device()
    if isinstance(devices, list):
        return devices[0]
    else:
        return devices


@PublicAPI(stability="alpha")
def get_devices() -> Optional[List[int]]:
    """Returns the device ID of the current Ray Train worker.

    This method returns the device index of the current GPU Worker. Returns None
    if called in a CPU worker. Note that you can only call this method inside
    the training function of :class:`TorchTrainer <ray.train.torch.TorchTrainer>`.
    """
    device = get_worker_root_device()
    if device.index is not None:
        return [device.index]
    else:
        return None


@PublicAPI(stability="alpha")
class RayDDPStrategy(DDPStrategy):
    """Subclass of DDPStrategy to ensure compatibility with Ray orchestration."""

    @property
    def root_device(self) -> torch.device:
        return get_worker_root_device()

    @property
    def distributed_sampler_kwargs(self) -> Dict[str, Any]:
        return dict(
            num_replicas=self.world_size,
            rank=self.global_rank,
        )


@PublicAPI(stability="alpha")
class RayFSDPStrategy(FSDPStrategy):
    """Subclass of FSDPStrategy to ensure compatibility with Ray orchestration."""

    @property
    def root_device(self) -> torch.device:
        return get_worker_root_device()

    @property
    def distributed_sampler_kwargs(self) -> Dict[str, Any]:
        return dict(
            num_replicas=self.world_size,
            rank=self.global_rank,
        )


@PublicAPI(stability="alpha")
class RayDeepSpeedStrategy(DeepSpeedStrategy):
    """Subclass of DeepSpeedStrategy to ensure compatibility with Ray orchestration."""

    def setup_distributed(self):
        # We have to set the device ids for each node
        # e.g. CUDA_VISIBLE_DEVICES = 2,3
        # worker 0: LOCAL_RANK=0, parallel devices = [cuda:0, cuda:1]
        # worker 1: LOCAL_RANK=1, parallel devices = [cuda:0, cuda:1]
        self.parallel_devices = [
            torch.device(f"cuda:{i}") for i in range(torch.cuda.device_count())
        ]
        super().setup_distributed()

    @property
    def root_device(self) -> torch.device:
        return get_worker_root_device()

    @property
    def distributed_sampler_kwargs(self) -> Dict[str, Any]:
        return dict(
            num_replicas=self.world_size,
            rank=self.global_rank,
        )


@PublicAPI(stability="alpha")
class RayLightningEnvironment(LightningEnvironment):
    """Setup Lightning DDP training environment for Ray cluster."""

    def world_size(self) -> int:
        return session.get_world_size()

    def global_rank(self) -> int:
        return session.get_world_rank()

    def local_rank(self) -> int:
        return session.get_local_rank()

    def node_rank(self) -> int:
        return session.get_node_rank()

    def set_world_size(self, size: int) -> None:
        # Disable it since `world_size()` directly returns data from AIR session.
        pass

    def set_global_rank(self, rank: int) -> None:
        # Disable it since `global_rank()` directly returns data from AIR session.
        pass

    def teardown(self):
        pass


class RayIterableDataset(IterableDataset):
    def __init__(self, dataset: "DataIterator", config: Dict[str, Any]) -> None:
        super().__init__()
        self.dataset = dataset
        self.config = config

    def __iter__(self):
        return self.dataset.iter_torch_batches(**self.config)


class RayDataModule(pl.LightningDataModule):
    def __init__(
        self,
        dataset_iter_config: Dict[str, Any],
        train_dataset: "DataIterator",
        val_dataset: Optional["DataIterator"] = None,
    ) -> None:
        super().__init__()

        def _train_dataloader() -> DataLoader:
            assert train_dataset
            ds = RayIterableDataset(train_dataset, dataset_iter_config)
            return DataLoader(ds, batch_size=1, collate_fn=lambda x: x[0])

        def _val_dataloader() -> DataLoader:
            assert val_dataset
            ds = RayIterableDataset(val_dataset, dataset_iter_config)
            return DataLoader(ds, batch_size=1, collate_fn=lambda x: x[0])

        if train_dataset:
            self.train_dataloader = _train_dataloader

        # ``pl.Trainer`` checks if the val_dataloader method has been overridden
        # to determine whether to enable the validation loop. To align with this
        # setting, we only override this method when `val_dataset` is not `None`.
        if val_dataset:
            self.val_dataloader = _val_dataloader


@PublicAPI(stability="alpha")
class RayModelCheckpoint(ModelCheckpoint):
    """
    AIR customized ModelCheckpoint callback.

    A subclass of ``pytorch_lightning.callbacks.ModelCheckpoint``.
    This callback function reports the latest metrics to the AIR session and
    creates an AIR checkpoint whenever a lightning checkpoint is saved.
    """

    def setup(
        self,
        trainer: "pl.Trainer",
        pl_module: "pl.LightningModule",
        stage: Optional[str] = None,
    ) -> None:
        super().setup(trainer, pl_module, stage)
        self.is_checkpoint_step = False

        if isinstance(trainer.strategy, DeepSpeedStrategy):
            # For DeepSpeed, each node has a unique set of param and optimizer states,
            # so the local rank 0 workers report the checkpoint shards for all workers
            # on their node.
            self.is_report_rank = session.get_local_rank() == 0
        else:
            # For DDP and FSDP, only the global rank 0 worker saves the full model.
            # Therefore, it is the only one that needs to report checkpoints.
            self.is_report_rank = session.get_world_rank() == 0

    def _session_report(self, trainer: "pl.Trainer", stage: str):
        """Report latest metrics dict and checkpoint to AIR training session.

        This method is called whenever a new checkpoint is created. It creates
        a `LightningCheckpoint` and reports it to the AIR session along with
        the latest metrics.
        """

        # Align the frequency of checkpointing and logging
        if not self.is_checkpoint_step:
            return

        # Report latest logged metrics
        metrics = {LIGHTNING_REPORT_STAGE_KEY: stage}
        for k, v in self._monitor_candidates(trainer).items():
            if isinstance(v, torch.Tensor):
                metrics[k] = v.item()

        # Ensures all workers already finish writing their checkpoints
        trainer.strategy.barrier()

        # Create and report the latest checkpoint
        with tempfile.TemporaryDirectory() as tmpdir:
            src_model_path = os.path.expanduser(self.last_model_path)
            dst_model_path = os.path.join(tmpdir, MODEL_KEY)

            # Copy the lightning ckpt into a tmp directory
            # - File ckpt:       last.ckpt   -> checkpoint_00000x/model
            # - Directory ckpt:  last.ckpt/* -> checkpoint_00000x/model/*
            if self.is_report_rank:
                if os.path.isdir(src_model_path):
                    shutil.copytree(src_model_path, dst_model_path)
                elif os.path.isfile(src_model_path):
                    shutil.copy(src_model_path, dst_model_path)

            # Only the report_rank worker creates the actual checkpoints.
            # Other workers create placeholder checkpoints to prevent blocking.
            checkpoint = LightningCheckpoint.from_directory(path=tmpdir)
            session.report(metrics=metrics, checkpoint=checkpoint)

        self.is_checkpoint_step = False

    def _save_last_checkpoint(self, *args, **kwargs) -> None:
        super()._save_last_checkpoint(*args, **kwargs)
        self.is_checkpoint_step = True

    def on_train_batch_end(self, trainer: "pl.Trainer", *args, **kwargs) -> None:
        super().on_train_batch_end(trainer, *args, **kwargs)
        self._session_report(trainer=trainer, stage="train_batch_end")

    def on_train_epoch_end(self, trainer: "pl.Trainer", *args, **kwargs) -> None:
        super().on_train_epoch_end(trainer, *args, **kwargs)
        self._session_report(trainer=trainer, stage="train_epoch_end")

    def on_validation_end(self, trainer: "pl.Trainer", *args, **kwargs) -> None:
        super().on_validation_end(trainer, *args, **kwargs)
        self._session_report(trainer=trainer, stage="validation_end")


@PublicAPI(stability="alpha")
def prepare_trainer(trainer: pl.Trainer) -> pl.Trainer:
    # Check strategy class
    valid_strategy_class = [RayDDPStrategy, RayFSDPStrategy, RayFSDPStrategy]

    if not any(isinstance(trainer.strategy, cls) for cls in valid_strategy_class):
        raise RuntimeError(
            f"Invalid strategy class: {type(trainer.strategy)}. To use Lightning with Ray, "
            "You have to provide one of [RayDDPStrategy, RayFSDPStrategy, RayDeepspeedStrategy] "
            "or its subclass to `pytorch_lightning.Trainer(strategy=)`!"
        )

    # Check cluster environment
    cluster_environment = getattr(trainer.strategy, "cluster_environment", None)
    if cluster_environment and not isinstance(
        cluster_environment, RayLightningEnvironment
    ):
        raise RuntimeError(
            "Invalid cluster environment plugin. The expected class is"
            f"`ray.train.lightning.RayLightningEnvironment` but get {type(cluster_environment)}!"
        )

    # Check model callbacks
    ray_checkpoint_callbacks = [
        cb for cb in trainer.checkpoint_callbacks if isinstance(cb, RayModelCheckpoint)
    ]
    if len(ray_checkpoint_callbacks) > 1:
        raise RuntimeError(
            "You can provide at most one RayModelCheckpoint callbacks for Ray Train, but "
            f"got {len(ray_checkpoint_callbacks)} here. For additional checkpoint callbacks, "
            "please use the original `pl.callbacks.ModelCheckpoint` class instead!"
        )

    return trainer
