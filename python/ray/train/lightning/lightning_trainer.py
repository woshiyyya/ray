import pytorch_lightning as pl
from pytorch_lightning.callbacks import ModelCheckpoint
from pytorch_lightning.strategies.ddp import DDPStrategy

from typing import TYPE_CHECKING, Callable, Dict, Optional, Union, Type, Any
from inspect import isclass
from pytorch_lightning import strategies
from pytorch_lightning.core import datamodule
from pytorch_lightning.plugins.environments import ClusterEnvironment

import ray
from ray.air.checkpoint import Checkpoint
from ray.air.config import DatasetConfig, RunConfig, ScalingConfig
from ray.train.data_parallel_trainer import DataParallelTrainer
from ray.train.torch.config import TorchConfig
from ray.train.trainer import GenDataset
from ray.util import PublicAPI
from ray.air import CheckpointConfig, session
from ray.train.constants import (
    EVALUATION_DATASET_KEY,
    TRAIN_DATASET_KEY,
)

from ray.train.lightning.lightning_utils import RayDDPStrategy, RayEnvironment, RayModelCheckpoint

# if TYPE_CHECKING:
from ray.data.preprocessor import Preprocessor

LIGHTNING_MODULE_KEY = "_lightning_module"
LIGHTNING_MODULE_CONFIG_KEY = "_lightning_module_config"
LIGHTNING_TRAINER_CONFIG_KEY = "_lightning_trainer_config"
MODEL_CHECKPOINT_CONFIG = "_model_checkpoint_config"
DDP_STRATEGY_CONFIG_KEY = "_ddp_strategy_config"
LIGHTNING_DATAMODULE_KEY = "_lightning_datamodule"

@PublicAPI(stability="alpha")
class LightningTrainer(DataParallelTrainer):
    def __init__(
        self,
        lightning_module: pl.LightningModule,
        *,
        lightning_module_config: Optional[Dict] = None,
        lightning_trainer_config: Optional[Dict] = None,
        ddp_strategy_config: Optional[Dict] = None,
        model_checkpoint_config: Optional[Dict] = None,
        torch_config: Optional[TorchConfig] = None,
        scaling_config: Optional[ScalingConfig] = None,
        dataset_config: Optional[Dict[str, DatasetConfig]] = None,
        run_config: Optional[RunConfig] = None,
        datasets: Optional[Dict[str, GenDataset]] = None,
        datamodule: Optional[pl.LightningDataModule] = None,
        preprocessor: Optional[Preprocessor] = None,
        resume_from_checkpoint: Optional[Checkpoint] = None,
    ):
        if not torch_config:
            torch_config = TorchConfig()

        train_loop_config = self._create_trainer_loop_config(
            lightning_module, lightning_module_config, lightning_trainer_config, ddp_strategy_config, model_checkpoint_config, datamodule
        )

        super(LightningTrainer, self).__init__(
            train_loop_per_worker=_lightning_train_loop_per_worker,
            train_loop_config=train_loop_config,
            backend_config=torch_config,
            scaling_config=scaling_config,
            dataset_config=dataset_config,
            run_config=run_config,
            datasets=datasets,
            preprocessor=preprocessor,
            resume_from_checkpoint=resume_from_checkpoint,
        )

    @classmethod
    def _create_trainer_loop_config(
        cls,
        lightning_module: pl.LightningModule,
        lightning_module_config: Optional[Dict] = None,
        lightning_trainer_config: Optional[Dict] = None,
        ddp_strategy_config: Optional[Dict] = None,
        model_checkpoint_config: Optional[Dict] = None,
        datamodule: Optional[pl.LightningDataModule] = None,
    ) -> Dict[str, Any]:

        trainer_loop_config = {}
        if not isclass(lightning_module):
            raise ValueError(
                "'lightning_module' must be a class, not a class instance."
            )
        if not issubclass(lightning_module, pl.LightningModule):
            raise ValueError(
                "'lightning_module' must be a subclass of "
                "'pytorch_lightning.LightningModule'"
            )
        trainer_loop_config[LIGHTNING_MODULE_KEY] = lightning_module

        if lightning_module_config:
            trainer_loop_config[LIGHTNING_MODULE_CONFIG_KEY] = lightning_module_config

        if lightning_trainer_config:
            trainer_loop_config[LIGHTNING_TRAINER_CONFIG_KEY] = lightning_trainer_config

        if ddp_strategy_config:
            trainer_loop_config[DDP_STRATEGY_CONFIG_KEY] = ddp_strategy_config

        if model_checkpoint_config:
            trainer_loop_config[MODEL_CHECKPOINT_CONFIG] = model_checkpoint_config
        
        if datamodule:
            trainer_loop_config[LIGHTNING_DATAMODULE_KEY] = datamodule
        return trainer_loop_config


def _lightning_train_loop_per_worker(config):
    """Per-worker training loop for HuggingFace Transformers."""

    datamodule = config.get(LIGHTNING_DATAMODULE_KEY, None)
    if not datamodule:
        # Build Datamodule with Ray Datasets
        train_dataset = session.get_dataset_shard(TRAIN_DATASET_KEY)
        eval_dataset = session.get_dataset_shard(EVALUATION_DATASET_KEY)
        # datamodule = build_data_module(train_dataset, eval_dataset, ...)

    LightningModuleCls = config.pop(LIGHTNING_MODULE_KEY)
    module_init_config = config.get(LIGHTNING_MODULE_CONFIG_KEY, {})
    lightning_module = LightningModuleCls(**module_init_config)

    trainer_config = config.get(LIGHTNING_TRAINER_CONFIG_KEY, {})
    trainer_config["enable_progress_bar"] = True
    trainer_config["enable_checkpointing"] = True

    # set trainer's parallel devices
    current_device = ray.train.torch.get_device()
    trainer_config["devices"] = [current_device.index]

    # set ray cluster env
    trainer_config["plugins"] = [plugin for plugin in trainer_config.get(
        "plugins", []) if not isinstance(plugin, ClusterEnvironment)]
    trainer_config["plugins"].append(RayEnvironment())

    # Setup ddp strategy
    ddp_strategy_config = config.get(DDP_STRATEGY_CONFIG_KEY, {})
    trainer_config["strategy"] = RayDDPStrategy(**ddp_strategy_config)

    # Insert RayModelCheckpoint Callback
    model_checkpoint_config = config.get(MODEL_CHECKPOINT_CONFIG, {})
    trainer_config["callbacks"] = [callback for callback in trainer_config.get(
        "callbacks", []) if not isinstance(callback, ModelCheckpoint)]
    trainer_config["callbacks"].append(RayModelCheckpoint(**model_checkpoint_config))

    trainer = pl.Trainer(**trainer_config)
    trainer.fit(lightning_module, datamodule=datamodule)
