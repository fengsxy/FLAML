import hashlib
import os,json
import random

import torch
import transformers
import wandb

from .dataset.dataprocess_auto import AutoToEncoded
from .dataset.sentence_keys_auto import get_sentence_keys

transformers.logging.set_verbosity_error()
import numpy as np

from ray.tune import CLIReporter

import time
import ray
import datasets
from datasets import load_dataset
from transformers.trainer_utils import IntervalStrategy, HPSearchBackend

from transformers import AutoTokenizer, AutoModelForSequenceClassification, AutoConfig, TrainingArguments

from .dataset.metric_auto import get_default_and_alternative_metric
from .dataset.submission_auto import auto_output_prediction
from .dataset.task_auto import get_default_task
from .hpo.grid_searchspace_auto import AutoGridSearchSpace
from .hpo.hpo_searchspace import AutoHPOSearchSpace, HPO_SEARCH_SPACE_MAPPING, hp_type_mapping
from .huggingface.switch_head_auto import AutoSeqClassificationHead, MODEL_CLASSIFICATION_HEAD_MAPPING
from .utils import PathUtils, _variable_override_default_alternative
from .hpo.searchalgo_auto import AutoSearchAlgorithm, SEARCH_ALGO_MAPPING
from .hpo.scheduler_auto import SCHEDULER_MAPPING, AutoScheduler
from .result_analysis.azure_utils import JobID

from .huggingface.trainer import TrainerForAutoTransformers

import logging
logger = logging.getLogger(__name__)
logger_formatter = logging.Formatter(
    '[%(name)s: %(asctime)s] {%(lineno)d} %(levelname)s - %(message)s',
    '%m-%d %H:%M:%S')

task_list = [
    "seq-classification",
    "regression",
    "question-answering"
]

class AutoTransformers:

    '''The AutoTransformers class

    Example:

        .. code-block:: python

            autohf = AutoTransformers()
            autohf_settings = {"metric_name": "accuracy",
                   "mode_name": "max",
                   "resources_per_trial": {"gpu": 4, "cpu": 4},
                   "search_algo_name": method,
                   "num_samples": 4,
                   "time_budget": 7200,
                   "points_to_evaluate": [{
                       "num_train_epochs": 1,
                       "per_device_train_batch_size": 128, }]
                   }

            autohf.fit(train_dataset,
                       eval_dataset,
                       **autohf_settings,)

    '''

    # _task_name: str = field(metadata={"help": "The task name, e.g., text-classification, question-answering"})
    # _dataset_name: list = field(metadata={"help": "The dataset name, e.g., glue"})
    # _subdataset_name: Optional[str] = field(metadata={"help": "The subdataset name if there's any, e.g., mnli"})
    # _model_type: str = field(metadata={"help": "The model type, e.g., bert, roberta, etc."})
    # _split_mode: str = field(metadata={"help": "The split mode of the dataset, it can only be resplit or origin"})
    #
    # _scheduler_name: str = field(metadata={"help": "The scheduler name."})
    # _search_algo_name: str = field(metadata={"help": "The hpo method name."})
    #
    # _metric_name: str = field(metadata={"help": "metric name"})
    # _metric_mode_name: str = field(metadata={"help": "metric mode name"})
    #
    # _max_seq_length: Optional[int] = field(metadata={"help": "max seq length"})
    # _fp16: Optional[bool] = field(metadata={"help": "is fp16"})
    #
    # # the following arguments are specific to text classification
    # _num_labels: Optional[int] = field(metadata={"help": "The number of labels of output classes"})

    @staticmethod
    def _convert_dict_to_ray_tune_space(config_json, mode ="grid"):
        search_space = {}

        if mode == "grid":
            for each_hp in config_json.keys():
                this_config = config_json[each_hp]
                assert isinstance(this_config, dict) or isinstance(this_config, list), "config of " + each_hp + " must be dict or list"
                search_space[each_hp] = ray.tune.grid_search(this_config)
        else:
            for each_hp in config_json.keys():
                this_config = config_json[each_hp]
                assert isinstance(this_config, dict) or isinstance(this_config, list), "config of " + each_hp + " must be dict or list"
                if isinstance(this_config, dict):
                    lower = this_config["l"]
                    upper = this_config["u"]
                    space = this_config["space"]
                    if space == "log":
                        search_space[each_hp] = ray.tune.loguniform(lower, upper)
                    elif space == "linear":
                        search_space[each_hp] = ray.tune.uniform(lower, upper)
                    elif space == "quniform":
                        search_space[each_hp] = ray.tune.quniform(lower, upper, this_config["interval"])
                else:
                    search_space[each_hp] = ray.tune.choice(this_config)

        return search_space

    def _set_search_space(self,
                          **custom_hpo_args):
        search_space_dict_hpo = search_space_dict_grid = None
        if self.jobid_config.mod == "grid":
            search_space_grid_json = AutoGridSearchSpace.from_model_and_dataset_name(self.jobid_config.pre, self.jobid_config.presz, self.jobid_config.dat[0], self.jobid_config.subdat, "grid")
            search_space_dict_grid = AutoTransformers._convert_dict_to_ray_tune_space(search_space_grid_json, mode="grid")
            search_space_dict_hpo = search_space_dict_grid
        if self.jobid_config.mod != "grid" and self.jobid_config.mod != "gridbert":
            search_space_hpo_json = AutoHPOSearchSpace.from_model_and_dataset_name(logger, self.jobid_config.spa, self.jobid_config.pre, self.jobid_config.presz, self.jobid_config.dat[0], self.jobid_config.subdat, **custom_hpo_args)
            search_space_dict_hpo = AutoTransformers._convert_dict_to_ray_tune_space(search_space_hpo_json, mode="hpo")
        elif self.jobid_config.mod == "gridbert":
            search_space_hpo_json = AutoGridSearchSpace.from_model_and_dataset_name("bert", "base", self.jobid_config.dat[0], self.jobid_config.subdat, "grid")
            search_space_dict_hpo = AutoTransformers._convert_dict_to_ray_tune_space(search_space_hpo_json, mode="grid")

        search_space_dict_hpo = TrainerForAutoTransformers.resolve_hp_conflict(search_space_dict_hpo)
        self._search_space_hpo = search_space_dict_hpo
        if self.jobid_config.mod == "grid":
            search_space_dict_grid = TrainerForAutoTransformers.resolve_hp_conflict(search_space_dict_grid)
            self._search_space_grid = search_space_dict_grid
        else:
            self._search_space_grid = None

        try:
            self.ds_config = custom_hpo_args["ds_config"]
        except KeyError:
            self.ds_config = None

    def _wrapper(self, func, *args):  # with star
        return func(*args)

    def _get_split_name(self, data_raw, fold_name = None):
        if fold_name:
            return fold_name
        fold_keys = data_raw.keys()
        if fold_keys == {"train", "validation", "test"}:
            return "train", "validation", "test"
        for each_key in fold_keys:
            for each_split_name in {"train", "validation", "test"}:
                assert not (each_key.startswith(each_split_name) and each_key != each_split_name), \
                    "Dataset split must be within {}, must be explicitly specified in dataset_config, e.g.," \
                    "'fold_name': ['train', 'validation_matched', 'test_matched']. Please refer to the example in the " \
                    "documentation of AutoTransformers.prepare_data()".format(",".join(fold_keys))
        return "train", "validation", "test"

    def prepare_data(self,
                     server_name,
                     data_root_path,
                     jobid_config,
                     wandb_utils,
                     max_seq_length = 128,
                     fold_name = None,
                     resplit_portion=None):
        '''Prepare data

            Args:
                dataset_config:
                    a dict for data specification, it must contain two keys:
                        -- "task": the task name, e.g., "text-classification" "question-answering"
                        -- "dataset_name": the dataset name, must be one of the dataset name in huggingface, or "custom"
                        -- "input_path": the custom path for specifying the custom dataset, must be specified if dataset_name = "custom"
                        -- "subdataset_name": the sub dataset name, e.g., "glue", "qnli". Not required.
                    e.g., {"task": "text-classification",
                            "dataset_name": ["glue"],
                            "subdataset_name": "rte",
                            "folder_name": }

                model_name:
                    the huggingface name path under huggingface.co/models
                    e.g., "google/grid-base-discriminator"
                split_mode:
                    the mode for splitting the dataset, must be one of two:
                    -- "resplit": mixing train and dev, then resplit them into a proportion defined by the resplit_portion parameter, this
                     mode is mostly for resplitting glue after considering the overfitting problem in the few-sample subdatasets, e.g., RTE, MRPC, SST, QNLI
                    -- "origin": keep the original train/dev/test split.
                ckpt_path:
                    the root path for outputting the checkpoints
                result_path:
                    the root path for outputting the result
                log_path:
                    the root path for saving the log
                max_seq_length:
                    max_seq_length for the huggingface, this hyperparameter must be specified at the data processing step
                resplit_portion:
                    the proportion for resplitting the train and dev data when split_mode="resplit". Not required.
            '''
        self._max_seq_length = max_seq_length
        self._server_name = server_name
        self.jobid_config = jobid_config
        self.wandb_utils = wandb_utils

        self.path_utils = PathUtils(jobid_config, hpo_data_root_path = data_root_path)

        if jobid_config.spt == "rspt":
            assert resplit_portion, "If split mode is 'rspt', the resplit_portion must be provided. Please " \
                                    "refer to the example in the documentation of AutoTransformers.prepare_data()"
        if jobid_config.subdat:
            data_raw = load_dataset(jobid_config.dat[0], jobid_config.subdat)
        else:
            data_raw = self._wrapper(load_dataset, *jobid_config.dat)

        self._train_name, self._dev_name, self._test_name = self._get_split_name(data_raw, fold_name=fold_name)
        auto_tokentoids_config = {"max_seq_length": self._max_seq_length}
        self._tokenizer = AutoTokenizer.from_pretrained(jobid_config.pre_full, use_fast=True)

        data_encoded = AutoToEncoded.from_model_and_dataset_name(data_raw,jobid_config.pre_full,jobid_config.dat[0], jobid_config.subdat, **auto_tokentoids_config)
        self._max_seq_length = 0
        for each_fold in data_encoded.keys():
            self._max_seq_length = max(self._max_seq_length,
                max([sum(data_encoded[each_fold][x]['attention_mask']) for x in range(len(data_encoded[each_fold]))]))
        self._max_seq_length = int((self._max_seq_length + 15) / 16) * 16
        data_encoded = AutoToEncoded.from_model_and_dataset_name(data_raw, jobid_config.pre_full, jobid_config.dat[0], jobid_config.subdat,**auto_tokentoids_config)

        if jobid_config.spt == "rspt":
            all_folds_from_source = []
            assert "source" in resplit_portion.keys(), "Must specify the source for resplitting the dataset in" \
            "resplit_portion, which is a list of folder names, e.g., resplit_portion = {'source': ['train']}"

            source_fold_names = resplit_portion['source']
            for each_fold_name in source_fold_names:
                this_fold_dataset = data_encoded[each_fold_name]
                all_folds_from_source.append(this_fold_dataset)

            merged_folds_from_source = datasets.concatenate_datasets(all_folds_from_source)
            merged_folds_from_source = merged_folds_from_source.shuffle(seed=42)

            assert "train" in resplit_portion.keys() and "validation" in resplit_portion.keys() \
                and "test" in resplit_portion.keys(), "train, validation, test must exist in resplit_portion"

            for key in ["train", "validation", "test"]:
                target_fold_start, target_fold_end = int(resplit_portion[key][0] * len(merged_folds_from_source)), \
                        int(resplit_portion[key][1] * len(merged_folds_from_source))
                if key == "train":
                    self.train_dataset = merged_folds_from_source.select([x for x in range(target_fold_start, target_fold_end)]).flatten_indices()
                elif key == "validation":
                    self.eval_dataset = merged_folds_from_source.select([x for x in range(target_fold_start, target_fold_end)]).flatten_indices()
                else:
                    self.test_dataset = merged_folds_from_source.select([x for x in range(target_fold_start, target_fold_end)]).flatten_indices()
        else:
            self.train_dataset, self.eval_dataset, self.test_dataset = data_encoded[self._train_name], data_encoded[self._dev_name], data_encoded[
                self._test_name]

    def _set_model_config(self, checkpoint_path, per_model_config, model_config_num_labels):
        if per_model_config and len(per_model_config) > 0:
            model_config = AutoConfig.from_pretrained(
                checkpoint_path,
                num_labels=model_config_num_labels,
                **per_model_config)
        else:
            model_config = AutoConfig.from_pretrained(
                checkpoint_path,
                num_labels=model_config_num_labels)
        return model_config

    def  _load_model(self,
                    checkpoint_path = None,
                    per_model_config=None):
        this_task = get_default_task(self.jobid_config.dat[0], self.jobid_config.subdat)
        if this_task == "seq-classification":
            self._num_labels = len(self.train_dataset.features["label"].names)
        elif this_task == "regression":
            self._num_labels = 1

        if not checkpoint_path:
            checkpoint_path = self.jobid_config.pre_full
        if this_task == "seq-classification":
            num_labels_old = AutoConfig.from_pretrained(checkpoint_path).num_labels
            if self.jobid_config.pre in MODEL_CLASSIFICATION_HEAD_MAPPING.keys():
                model_config_num_labels = num_labels_old
            else:
                model_config_num_labels = self._num_labels
            model_config = self._set_model_config(checkpoint_path, per_model_config, model_config_num_labels)

            if self.jobid_config.pre in MODEL_CLASSIFICATION_HEAD_MAPPING.keys():
                num_labels_old = AutoConfig.from_pretrained(checkpoint_path).num_labels
                if per_model_config and len(per_model_config) > 0:
                    model_config = AutoConfig.from_pretrained(
                        checkpoint_path,
                        num_labels = num_labels_old,
                        **per_model_config)
                else:
                    model_config = AutoConfig.from_pretrained(
                        checkpoint_path,
                        num_labels = num_labels_old)

                if self._num_labels != num_labels_old:
                    model_config.num_labels = num_labels_old
                    this_model = AutoModelForSequenceClassification.from_pretrained(checkpoint_path, config=model_config)
                    model_config.num_labels = self._num_labels
                    this_model.num_labels = self._num_labels
                    this_model.classifier = AutoSeqClassificationHead.from_model_type_and_config(self.jobid_config.pre, model_config)
                else:
                    this_model = AutoModelForSequenceClassification.from_pretrained(checkpoint_path, config=model_config)
            else:
                this_model = AutoModelForSequenceClassification.from_pretrained(checkpoint_path, config=model_config)

            this_model.resize_token_embeddings(len(self._tokenizer))
            return this_model
        else:
            model_config = self._set_model_config(checkpoint_path, per_model_config, 1)
            this_model = AutoModelForSequenceClassification.from_pretrained(checkpoint_path, config=model_config)
            return this_model

    def _get_metric_func(self):
        if self.jobid_config.dat[0] in ("glue", "super_glue"):
            metric = datasets.load.load_metric(self.jobid_config.dat[0], self.jobid_config.subdat)
        elif self.jobid_config.dat[0] in ("squad", "squad_v2"):
            metric = datasets.load.load_metric(self.jobid_config.dat[0])
        else:
            metric = datasets.load.load_metric(self.metric_name)
        return metric

    def _compute_metrics_by_dataset_name(self,
                                         eval_pred):
        predictions, labels = eval_pred
        predictions = np.squeeze(predictions) \
            if self.task_name == "regression" else np.argmax(predictions, axis=1)
        metric_func = self._get_metric_func()
        return metric_func.compute(predictions=predictions, references=labels)

    def _compute_checkpoint_freq(self,
                                 num_train_epochs,
                                 batch_size,
                                 mode="last"):
        assert mode in {"last"}
        if "gpu" in self._resources_per_trial:
            ckpt_step_freq = int(min(num_train_epochs, 1) * len(self.train_dataset) / batch_size /
                                 self._resources_per_trial["gpu"] / self.ckpt_per_epoch) + 1
        else:
            ckpt_step_freq = int(min(num_train_epochs, 1) * len(self.train_dataset) / batch_size /
                                 self._resources_per_trial["cpu"] / self.ckpt_per_epoch) + 1

        return ckpt_step_freq

    @staticmethod
    def _separate_config(config):
        training_args_config = {}
        per_model_config = {}

        for key in config.keys():
            if key in TrainingArguments.__dict__.keys():
                training_args_config[key] = config[key]
            else:
                per_model_config[key] = config[key]

        return training_args_config, per_model_config

    def _objective(self, config, reporter, checkpoint_dir=None):
        def model_init():
            return self._load_model()
        from transformers.trainer_utils import set_seed
        set_seed(config["seed"])
        np.random.seed(config["seed"])
        torch.manual_seed(config["seed"])
        torch.cuda.manual_seed(config["seed"])
        np.random.seed(config["seed"])
        random.seed(config["seed"])

        if "warmup_ratio" in config.keys() and config["warmup_ratio"] > 1:
            config["warmup_steps"] = config["warmup_ratio"]
            config["warmup_ratio"] = 0

        if "num_train_epochs" in config.keys() and config["num_train_epochs"] > 100:
            config["max_steps"] = config["num_train_epochs"]
            config["num_train_epochs"] = 0

        training_args_config, per_model_config = AutoTransformers._separate_config(config)
        this_model = self._load_model(per_model_config=per_model_config)

        trial_id = reporter.trial_id
        self.path_utils.make_dir_per_trial(trial_id)

        ckpt_freq = self._compute_checkpoint_freq(
            num_train_epochs = config["num_train_epochs"],
            batch_size = config["per_device_train_batch_size"],
            mode="last")

        assert self.path_utils.ckpt_dir_per_trial
        training_args = TrainingArguments(
            output_dir=self.path_utils.ckpt_dir_per_trial,
            do_eval=False,
            per_device_eval_batch_size=32,
            eval_steps= ckpt_freq,
            evaluation_strategy = IntervalStrategy.STEPS,
            save_steps= ckpt_freq,
            save_total_limit=0,
            fp16= self._fp16,
            deepspeed = self.ds_config,
            **training_args_config,
        )

        trainer = TrainerForAutoTransformers(
            this_model,
            training_args,
            model_init=model_init,
            train_dataset=self.train_dataset,
            eval_dataset=self.eval_dataset,
            tokenizer=self._tokenizer,
            compute_metrics= self._compute_metrics_by_dataset_name,
        )
        trainer.logger = logger
        trainer.trial_id = reporter.trial_id

        run = self.wandb_utils.set_wandb_per_trial()
        if os.environ["WANDB_MODE"] == "online":
            for each_hp in config:
                if each_hp in hp_type_mapping.keys():
                    wandb.log({each_hp: config[each_hp]})
        trainer.train()
        output_metrics = trainer.evaluate(self.eval_dataset)
        if run:
            run.finish()

    def _verify_init_config(self,
                            **custom_hpo_args):
        for key in custom_hpo_args.keys():
            if key == "points_to_evaluate":
                for each_init_config in custom_hpo_args[key]:
                   for each_hp in each_init_config.keys():
                       assert each_hp in self._search_space_hpo.keys(), \
                           "points_to_evaluate hp must be within the search space"

                       assert isinstance(each_init_config[each_hp], int) or \
                              isinstance(each_init_config[each_hp], float) or \
                              isinstance(each_init_config[each_hp], str) or \
                              isinstance(each_init_config[each_hp], bool), " points_to_evaluate must be a scalar"

                       assert isinstance(self._search_space_hpo[each_hp], ray.tune.sample.Categorical) or \
                              isinstance(self._search_space_hpo[each_hp], ray.tune.sample.Float)

                       if isinstance(self._search_space_hpo[each_hp], ray.tune.sample.Categorical):
                           assert each_init_config[each_hp] in self._search_space_hpo[each_hp].categories, \
                               f"points_to_evaluate {each_hp} value must be within the search space"
                       else:
                           assert each_init_config[each_hp] >= self._search_space_hpo[each_hp].lower \
                                  and each_init_config[each_hp] <= self._search_space_hpo[each_hp].upper, \
                               f"points_to_evaluate {each_hp} value must be within the search space"

    def _get_search_algo(self,
                         search_algo_name,
                         search_algo_args_mode,
                         **custom_hpo_args):
        if search_algo_name == "BlendSearch":
            self._verify_init_config(**custom_hpo_args)
        search_algo = AutoSearchAlgorithm.from_method_name(search_algo_name, search_algo_args_mode, self._search_space_hpo, **custom_hpo_args)
        return search_algo

    @staticmethod
    def _recover_checkpoint(tune_checkpoint_dir):
        assert tune_checkpoint_dir
        # Get subdirectory used for Huggingface.
        subdirs = [
            os.path.join(tune_checkpoint_dir, name)
            for name in os.listdir(tune_checkpoint_dir)
            if os.path.isdir(os.path.join(tune_checkpoint_dir, name))
        ]
        # There should only be 1 subdir.
        assert len(subdirs) == 1, subdirs
        return subdirs[0]

    def _save_ckpt_json(self,
                        best_ckpt):
        json.dump({"best_ckpt": best_ckpt}, open(os.path.join(self.path_utils.result_dir_per_run, "save_ckpt_" + self.jobid_config.to_jobid_string() + ".json"), "w"))

    def _save_output_metric(self,
                            output_metrics):
        json.dump(output_metrics, open(
            os.path.join(self.path_utils.result_dir_per_run, "output_metric_" + self.jobid_config.to_jobid_string() + ".json"), "w"))

    def _load_ckpt_json(self,
                        ckpt_dir = None,
                        **kwargs):
        if not ckpt_dir:
            ckpt_dir = os.path.join(self.path_utils.result_dir_per_run, "save_ckpt_" + self.jobid_config.to_jobid_string() + ".json")
        try:
            ckpt_json = json.load(open(ckpt_dir))
            return ckpt_json["best_ckpt"]
        except FileNotFoundError as err:
            logger.error("Saved checkpoint not found. Please make sure checkpoint is stored under {}".format(ckpt_dir))
            raise err

    def set_metric(self, custom_metric_name = None, custom_metric_mode_name = None):
        default_metric, default_mode, all_metrics, all_modes = get_default_and_alternative_metric(
                            self.jobid_config.dat[0],
                            subdataset_name=self.jobid_config.subdat,
                            custom_metric_name= custom_metric_name,
                            custom_metric_mode_name= custom_metric_mode_name)
        _variable_override_default_alternative(logger, self, "metric_name", default_metric, all_metrics, custom_metric_name)
        _variable_override_default_alternative(logger, self, "metric_mode_name", default_mode, all_modes, custom_metric_mode_name)
        self._all_metrics = all_metrics
        self._all_modes = all_modes

    def set_task(self):
        self.task_name = get_default_task(self.jobid_config.dat[0], self.jobid_config.subdat)

    def fit_hf(self,
               train_dataset,
               eval_dataset,
               resources_per_trial,
               num_samples,
               time_budget,
               custom_metric_name=None,
               custom_metric_mode_name=None,
               _fp16 = True,
               **custom_hpo_args
               ):
        def model_init():
            return self._load_model()
        def ray_hp_space(trial):
            return {
                "learning_rate": ray.tune.loguniform(1e-6, 1e-4),
                "num_train_epochs": ray.tune.choice(list(range(1, 6))),
                "seed": ray.tune.quniform(1, 41, 1),
                "per_device_train_batch_size": ray.tune.choice([4, 8, 16, 32, 64]),
            }

        self.set_metric(custom_metric_name, custom_metric_mode_name)
        self.set_task()

        training_args = TrainingArguments(
            output_dir=self.path_utils.hpo_ckpt_path,
            fp16=_fp16,
        )
        this_model = self._load_model()

        trainer = TrainerForAutoTransformers(
            this_model,
            training_args,
            model_init=model_init,
            train_dataset= train_dataset,
            eval_dataset= eval_dataset,
            tokenizer=self._tokenizer,
            compute_metrics=self._compute_metrics_by_dataset_name,
        )
        self.path_utils.make_dir_per_run()

        start_time = time.time()
        best_run = trainer.hyperparameter_search(
            n_trials = num_samples,
            time_budget_s= time_budget,
            hp_space = ray_hp_space,
            backend=HPSearchBackend.RAY,
            resources_per_trial = resources_per_trial)
        duration = time.time() - start_time
        self.last_run_duration = duration

        hp_dict = best_run.hyperparameters
        hp_dict["seed"] = int(hp_dict["seed"])

        best_training_args = TrainingArguments(
            output_dir=self.path_utils.hpo_ckpt_path,
            fp16=_fp16,
            **hp_dict,
        )

        best_trainer = TrainerForAutoTransformers(
            this_model,
            best_training_args,
            model_init=model_init,
            train_dataset=train_dataset,
            eval_dataset=eval_dataset,
            tokenizer=self._tokenizer,
            compute_metrics=self._compute_metrics_by_dataset_name,
        )

        best_model_checkpoint_path = os.path.join(self.path_utils.hpo_ckpt_path, "hpo_hf")
        if not os.path.exists(best_model_checkpoint_path):
            os.mkdir(best_model_checkpoint_path)
        best_trainer.train()
        best_trainer.save_model(best_model_checkpoint_path)
        self._save_ckpt_json(best_model_checkpoint_path)
        validation_metric = best_trainer.evaluate()

        return validation_metric

    def fit(self,
            resources_per_trial,
            num_samples,
            time_budget,
            custom_metric_name = None,
            custom_metric_mode_name = None,
            ckpt_per_epoch=1,
            fp16 = True,
            verbose = 1,
            **custom_hpo_args):
        '''Fine tuning the huggingface using the hpo setting

        Args:
            train_dataset:
                the training data of type datasets.Dataset, loaded from datasets.load_dataset
            eval_dataset:
                the validation data of type datasets.Dataset, loaded from datasets.load_dataset
            metric_name:
                A string of the dataset name or a function,
                e.g., 'accuracy', 'f1', 'loss',
                if passing a customized dataset function, the function needs to
                have the follwing signature:

                .. code-block:: python

                    def custom_metric(X_test, y_test, estimator, labels,
                     X_train, y_train, weight_test=None, weight_train=None):
                        return metric_to_minimize, metrics_to_log

                which returns a float number as the minimization objective,
                and a tuple of floats as the metrics to log
            metric_mode_name:
                A string of the mode name,
                e.g., "max", "min", "last", "all"
            resources_per_trial:
                A dict showing the resources used by each trial,
                e.g., {"gpu": 4, "cpu": 4}
            search_algo_name:
                The search algoritihm for AutoHF()
                e.g., "blendsearch" "cfo" "bo"
            search_space_path:
                a path for the json file for search space,
                e.g., search_space_path = "./hpo/grid/"
            scheduler_name:
                A string of the scheduler name,
                e.g., "ASHA", "HyperBand"
            num_samples:
                An int variable of the maximum number of trials
            time_budget:
                An int variable of the maximum time budget
            verbose:
                int, default=1 | Controls the verbosity, higher means more
                messages
            fp16:
                boolean, default = True | whether to use fp16
            search_algo_kwargs:
                The keyword arguments to be fed into the search algorith, e.g.,
                search_algo_kwargs = {"points_to_evaluate": [{
                           "num_train_epochs": 1,
                           "per_device_train_batch_size": 128, }]}
        '''
        self._resources_per_trial = resources_per_trial
        self.set_metric(custom_metric_name, custom_metric_mode_name)
        self.set_task()
        self._fp16 = fp16
        ray.init()

        self._set_search_space(**custom_hpo_args)
        search_algo = self._get_search_algo(self.jobid_config.alg, self.jobid_config.arg, **custom_hpo_args)
        scheduler = AutoScheduler.from_scheduler_name(self.jobid_config.pru)
        self.ckpt_per_epoch = ckpt_per_epoch
        self.path_utils.make_dir_per_run()

        logger.addHandler(logging.FileHandler(os.path.join(self.path_utils.log_dir_per_run, 'tune.log')))
        old_level = logger.getEffectiveLevel()
        self._verbose = verbose
        if verbose == 0:
            logger.setLevel(logging.WARNING)

        assert self.path_utils.ckpt_dir_per_run
        start_time = time.time()

        # Documentation on the wandb setting:
        # There are two ways to initialize wandb in tune.run:
        # (1) using WandbLoggerCallback, by adding the following argument to tune.run:
        #     callbacks=[WandbLoggerCallback(
        #                  project="hpo",
        #                  api_key = os.environ["WANDB_API_KEY"],
        #                  group = os.environ["WANDB_RUN_GROUP"],
        #                  log_config=True)]
        # (2) using wandb_mixin decorator (the current implementation)
        # The current implementation uses (2) because (1) has the following bug.
        # In Ray 1.2, when using WandbLoggerCallback + setting time limit using the time_budget_s argument,
        # A bug exists which is the previous run will not clear the cache after tune.run returns. After the
        # later run has already starts, some zombie trials in the previous run remain in the memory and never stop.
        # This bug can be reproduced by switching to (1) by adding the above callbacks argument and removing the wandb_mixin decorator
        # https://docs.ray.io/en/master/tune/tutorials/tune-wandb.html

        tune_config = self._search_space_hpo
        tune_config["seed"] = 42

        analysis = ray.tune.run(
            self._objective,
            metric= self.metric_name,
            mode = self.metric_mode_name,
            name = "ray_result",
            resources_per_trial = resources_per_trial,
            config= tune_config,
            verbose= verbose,
            local_dir= self.path_utils.ckpt_dir_per_run,
            num_samples = num_samples,
            time_budget_s= time_budget,
            keep_checkpoints_num = 1,
            scheduler= scheduler,
            search_alg= search_algo,
        )
        duration = time.time() - start_time
        self.last_run_duration = duration
        logger.info("Total running time: {} seconds".format(duration))

        ray.shutdown()

        best_trial = analysis.get_best_trial(scope="all", metric= self.metric_name, mode= self.metric_mode_name)
        validation_metric = {"eval_" + self.metric_name:
                    best_trial.metric_analysis[self.metric_name][self.metric_mode_name]}
        for x in range(len(self._all_metrics)):
            validation_metric["eval_" + self._all_metrics[x]] \
                = best_trial.metric_analysis[self._all_metrics[x]][self._all_modes[x]]

        get_best_ckpt = analysis.get_best_checkpoint(best_trial, metric= self.metric_name, mode= self.metric_mode_name)
        best_ckpt = AutoTransformers._recover_checkpoint(get_best_ckpt)

        self._save_ckpt_json(best_ckpt)

        if verbose==0:
            logger.setLevel(old_level)

        return validation_metric, analysis

    def predict(self,
                ckpt_json_dir = None,
                **kwargs):
        '''Predict label for test data.

        Args:
            test_dataset:
                the test dataset
            ckpt_json_dir:
                the checkpoint for the fine-tuned huggingface if you wish to override the saved checkpoint in the training stage under self.path_utils._result_dir_per_run

        Returns:
            A numpy array of shape n * 1 - - each element is a predicted class
            label for an instance.
        '''
        best_checkpoint = self._load_ckpt_json(ckpt_json_dir, **kwargs)
        best_model = self._load_model(checkpoint_path=best_checkpoint)
        training_args = TrainingArguments(per_device_eval_batch_size=1,
                                          output_dir=self.path_utils.result_dir_per_run)
        test_trainer = TrainerForAutoTransformers(best_model, training_args)

        if self.jobid_config.spt == "ori":
            try:
                self.test_dataset.remove_columns_("label")
            except ValueError:
                pass

        test_dataloader = test_trainer.get_test_dataloader(self.test_dataset)
        predictions, labels, _ = test_trainer.prediction_loop(test_dataloader, description="Prediction")
        predictions = np.squeeze(predictions) \
            if get_default_task(self.jobid_config.dat[0], self.jobid_config.subdat) == "regression" \
            else np.argmax(predictions, axis=1)
        torch.cuda.empty_cache()

        if self.jobid_config.spt == "rspt":
            assert labels is not None
            metric = self._get_metric_func()
            output_metric = metric.compute(predictions=predictions, references=labels)
            self._save_output_metric(output_metric)
            return predictions, output_metric
        else:
            return predictions, None

    def output_prediction(self,
                          predictions,
                          output_prediction_path,
                          output_dir_name):
        """
            Output prediction and prepare the submission file
        """
        return auto_output_prediction(self.jobid_config.dat[0], output_prediction_path,
                                      output_dir_name, predictions, self.train_dataset,
                                      self._dev_name, self.jobid_config.subdat)