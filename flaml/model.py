"""!
 * Copyright (c) Microsoft Corporation. All rights reserved.
 * Licensed under the MIT License.
"""

import numpy as np
import time
from sklearn.ensemble import RandomForestRegressor, RandomForestClassifier
from sklearn.ensemble import ExtraTreesRegressor, ExtraTreesClassifier
from sklearn.linear_model import LogisticRegression
from scipy.sparse import issparse
import pandas as pd
from . import tune
from .data import (
    group_counts,
    CLASSIFICATION,
    TS_FORECAST,
    TS_TIMESTAMP_COL,
    TS_VALUE_COL,
)

import logging
from typing import Union, List
from pandas import DataFrame, Series

logger = logging.getLogger("flaml.automl")


class BaseEstimator:
    """The abstract class for all learners

    Typical example:
        XGBoostEstimator: for regression
        XGBoostSklearnEstimator: for classification
        LGBMEstimator, RandomForestEstimator, LRL1Classifier, LRL2Classifier:
            for both regression and classification
    """

    def __init__(self, task="binary", **config):
        """Constructor

        Args:
            task: A string of the task type, one of
                'binary', 'multi', 'regression', 'rank', 'forecast'
            config: A dictionary containing the hyperparameter names, 'n_jobs' and 'resources_per_trial' as keys.
                n_jobs is the number of parallel threads. resources_per_trial is the number of gpus per trial.
                resources_per_trial is only used by TransformersEstimator.
        """
        self.params = self.config2params(config)
        self.estimator_class = self._model = None
        self._task = task
        if "_estimator_type" in config:
            self._estimator_type = self.params.pop("_estimator_type")
        else:
            self._estimator_type = (
                "classifier" if task in CLASSIFICATION else "regressor"
            )

    def get_params(self, deep=False):
        params = self.params.copy()
        params["task"] = self._task
        if hasattr(self, "_estimator_type"):
            params["_estimator_type"] = self._estimator_type
        return params

    @property
    def classes_(self):
        return self._model.classes_

    @property
    def n_features_in_(self):
        return self.model.n_features_in_

    @property
    def model(self):
        """Trained model after fit() is called, or None before fit() is called"""
        return self._model

    @property
    def estimator(self):
        """Trained model after fit() is called, or None before fit() is called"""
        return self._model

    def _preprocess(self, X):
        return X

    def _fit(self, X_train, y_train, **kwargs):

        current_time = time.time()
        if "groups" in kwargs:
            kwargs = kwargs.copy()
            groups = kwargs.pop("groups")
            if self._task == "rank":
                kwargs["group"] = group_counts(groups)
                # groups_val = kwargs.get('groups_val')
                # if groups_val is not None:
                #     kwargs['eval_group'] = [group_counts(groups_val)]
                #     kwargs['eval_set'] = [
                #         (kwargs['X_val'], kwargs['y_val'])]
                #     kwargs['verbose'] = False
                #     del kwargs['groups_val'], kwargs['X_val'], kwargs['y_val']
        X_train = self._preprocess(X_train)
        model = self.estimator_class(**self.params)
        if logger.level == logging.DEBUG:
            logger.debug(f"flaml.model - {model} fit started")
        model.fit(X_train, y_train, **kwargs)
        if logger.level == logging.DEBUG:
            logger.debug(f"flaml.model - {model} fit finished")
        train_time = time.time() - current_time
        self._model = model
        return train_time

    def fit(self, X_train, y_train, budget=None, **kwargs):
        """Train the model from given training data

        Args:
            X_train: A numpy array of training data in shape n*m
            y_train: A numpy array of labels in shape n*1
            budget: A float of the time budget in seconds

        Returns:
            train_time: A float of the training time in seconds
        """
        return self._fit(X_train, y_train, **kwargs)

    def predict(self, X_test, is_test=True):
        """Predict label from features

        Args:
            X_test: A numpy array of featurized instances, shape n*m

        Returns:
            A numpy array of shape n*1.
            Each element is the label for a instance
        """
        if self._model is not None:
            X_test = self._preprocess(X_test)
            return self._model.predict(X_test)
        else:
            return np.ones(X_test.shape[0])

    def predict_proba(self, X_test, is_test=True):
        """Predict the probability of each class from features

        Only works for classification problems

        Args:
            model: An object of trained model with method predict_proba()
            X_test: A numpy array of featurized instances, shape n*m

        Returns:
            A numpy array of shape n*c. c is the # classes
            Each element at (i,j) is the probability for instance i to be in
                class j
        """
        assert (
            self._task in CLASSIFICATION
        ), "predict_prob() only for classification task."
        X_test = self._preprocess(X_test)
        return self._model.predict_proba(X_test)

    def cleanup(self):
        pass

    @classmethod
    def search_space(cls, **params):
        """[required method] search space

        Returns:
            A dictionary of the search space.
            Each key is the name of a hyperparameter, and value is a dict with
                its domain (required) and low_cost_init_value, init_value,
                cat_hp_cost (if applicable).
                e.g.,
                {'domain': tune.randint(lower=1, upper=10), 'init_value': 1}.
        """
        return {}

    @classmethod
    def size(cls, config: dict) -> float:
        """[optional method] memory size of the estimator in bytes

        Args:
            config - A dict of the hyperparameter config.

        Returns:
            A float of the memory size required by the estimator to train the
            given config.
        """
        return 1.0

    @classmethod
    def cost_relative2lgbm(cls) -> float:
        """[optional method] relative cost compared to lightgbm"""
        return 1.0

    @classmethod
    def init(cls):
        """[optional method] initialize the class"""
        pass

    def config2params(self, config: dict) -> dict:
        """[optional method] config dict to params dict

        Args:
            config - A dict of the hyperparameter config.

        Returns:
            A dict that will be passed to self.estimator_class's constructor.
        """
        params = config.copy()
        if "resources_per_trial" in params:
            params.pop("resources_per_trial")
        return params


class TransformersEstimator(BaseEstimator):
    def __init__(self, task="seq-classification", resources_per_trial=None, **config):
        super().__init__(task, **config)
        self._resources_per_trial = resources_per_trial

    @classmethod
    def init(cls, hpo_method=None, metric_name=None, **kwargs):
        from .nlp.utils import HPOArgs

        custom_hpo_args = HPOArgs()
        for key, val in kwargs.items():
            if key in ("X_val", "y_val"):
                continue
            assert (
                key in custom_hpo_args.__dict__
            ), "The specified key {} is not in the argument list of flaml.nlp.utils::HPOArgs".format(
                key
            )
            setattr(custom_hpo_args, key, val)
        kwargs["custom_hpo_args"] = custom_hpo_args
        cls._metric_name = metric_name

    @classmethod
    def _preprocess(cls, X, **kwargs):
        from .nlp.utils import tokenize_text

        return tokenize_text(X, kwargs["custom_hpo_args"])

    def _split_train_val(self, X_train, y_train, **kwargs):
        if "X_val" in kwargs and "y_val" in kwargs:
            return X_train, y_train, kwargs["X_val"], kwargs["y_val"]
        n = max(int(len(y_train) * 0.9), len(y_train) - 1000)
        X_tr, y_tr = X_train[:n], y_train[:n]
        X_val, y_val = X_train[n:], y_train[n:]
        return X_tr, y_tr, X_val, y_val

    def fit(self, X_train: DataFrame, y_train: Series, budget=None, **kwargs):
        import pdb

        pdb.set_trace()
        import transformers
        from transformers import TrainingArguments
        from transformers.trainer_utils import set_seed
        from transformers import AutoTokenizer
        from .nlp.utils import (
            separate_config,
            load_model,
            get_num_labels,
            compute_checkpoint_freq,
        )
        from .nlp.huggingface.trainer import TrainerForAutoTransformers
        from datasets import Dataset

        X_train, y_train, X_val, y_val = self._split_train_val(X_train, **kwargs)
        if X_train.dtypes[0] == "string":
            X_train = self._preprocess(X_train, **kwargs)
            train_dataset = Dataset.from_pandas(X_train)
            X_val = self._preprocess(X_val, **kwargs)
            eval_dataset = Dataset.from_pandas(X_val)
        else:
            train_dataset = Dataset.from_pandas(X_train)
            eval_dataset = Dataset.from_pandas(X_val)

        tokenizer = AutoTokenizer.from_pretrained(
            kwargs["custom_hpo_args"].model_path, use_fast=True
        )
        set_seed(self.params["seed"])

        num_labels = get_num_labels(self._task, y_train)

        training_args_config, per_model_config = separate_config(self.params)
        this_model = load_model(
            checkpoint_path=kwargs["custom_hpo_args"].model_path,
            task=self._task,
            num_labels=num_labels,
            per_model_config=per_model_config,
        )
        ckpt_freq = compute_checkpoint_freq(
            self._resources_per_trial,
            train_data_size=len(X_train),
            custom_hpo_args=kwargs["custom_hpo_args"],
            num_train_epochs=self.params["num_train_epochs"],
            batch_size=self.params["batch_size"],
        )
        if transformers.__version__.startswith("3"):
            training_args = TrainingArguments(
                output_dir=kwargs["custom_hpo_args"].output_dir,
                do_train=True,
                do_eval=True,
                eval_steps=ckpt_freq,
                evaluate_during_training=True,
                save_steps=ckpt_freq,
                save_total_limit=0,
                fp16=kwargs["custom_hpo_args"].fp16,
                **training_args_config,
            )
        else:
            from transformers import IntervalStrategy

            training_args = TrainingArguments(
                output_dir=kwargs["custom_hpo_args"].output_dir,
                do_train=True,
                do_eval=True,
                per_device_eval_batch_size=1,
                eval_steps=ckpt_freq,
                evaluation_strategy=IntervalStrategy.STEPS,
                save_steps=ckpt_freq,
                save_total_limit=0,
                fp16=kwargs["custom_hpo_args"].fp16,
                **training_args_config,
            )

        trainer = TrainerForAutoTransformers(
            model=this_model,
            args=training_args,
            model_init=load_model,
            train_dataset=train_dataset,
            eval_dataset=eval_dataset,
            tokenizer=tokenizer,
            compute_metrics=self._compute_metrics_by_dataset_name,
        )
        trainer.train()
        self._best_ckpt_path = self._ckpt_to_metric

    def _compute_metrics_by_dataset_name(self, eval_pred):
        import datasets
        from .data import SEQREGRESSION

        predictions, labels = eval_pred
        predictions = (
            np.squeeze(predictions)
            if self._task == SEQREGRESSION
            else np.argmax(predictions, axis=1)
        )
        metric_func = datasets.load.load_metric(self._metric_name)
        return metric_func.compute(predictions=predictions, references=labels)

    def predict(self, X_test: Union[DataFrame, List[str], List[List[str]]], **kwargs):
        if isinstance(X_test, List) and isinstance(X_test[0], List):
            unzipped_X_test = [x for x in zip(*X_test)]
            X_test = DataFrame(
                {
                    "key_" + str(idx): unzipped_X_test[idx]
                    for idx in range(len(unzipped_X_test))
                }
            )
        elif isinstance(X_test, List):
            X_test = DataFrame(
                {"key_" + str(idx): [X_test[idx]] for idx in range(len(X_test))}
            )
        if X_test.dtypes[0] == "string":
            X_test = self._preprocess(X_test, **kwargs)


class SKLearnEstimator(BaseEstimator):
    def __init__(self, task="binary", **config):
        super().__init__(task, **config)

    def _preprocess(self, X):
        if isinstance(X, pd.DataFrame):
            cat_columns = X.select_dtypes(include=["category"]).columns
            if not cat_columns.empty:
                X = X.copy()
                X[cat_columns] = X[cat_columns].apply(lambda x: x.cat.codes)
        elif isinstance(X, np.ndarray) and X.dtype.kind not in "buif":
            # numpy array is not of numeric dtype
            X = pd.DataFrame(X)
            for col in X.columns:
                if isinstance(X[col][0], str):
                    X[col] = X[col].astype("category").cat.codes
            X = X.to_numpy()
        return X


class LGBMEstimator(BaseEstimator):
    @classmethod
    def search_space(cls, data_size, **params):
        upper = min(32768, int(data_size))
        return {
            "n_estimators": {
                "domain": tune.lograndint(lower=4, upper=upper),
                "init_value": 4,
                "low_cost_init_value": 4,
            },
            "num_leaves": {
                "domain": tune.lograndint(lower=4, upper=upper),
                "init_value": 4,
                "low_cost_init_value": 4,
            },
            "min_child_samples": {
                "domain": tune.lograndint(lower=2, upper=2 ** 7 + 1),
                "init_value": 20,
            },
            "learning_rate": {
                "domain": tune.loguniform(lower=1 / 1024, upper=1.0),
                "init_value": 0.1,
            },
            # 'subsample': {
            #     'domain': tune.uniform(lower=0.1, upper=1.0),
            #     'init_value': 1.0,
            # },
            "log_max_bin": {  # log transformed with base 2
                "domain": tune.lograndint(lower=3, upper=11),
                "init_value": 8,
            },
            "colsample_bytree": {
                "domain": tune.uniform(lower=0.01, upper=1.0),
                "init_value": 1.0,
            },
            "reg_alpha": {
                "domain": tune.loguniform(lower=1 / 1024, upper=1024),
                "init_value": 1 / 1024,
            },
            "reg_lambda": {
                "domain": tune.loguniform(lower=1 / 1024, upper=1024),
                "init_value": 1.0,
            },
        }

    def config2params(cls, config: dict) -> dict:
        params = super().config2params(config)
        if "log_max_bin" in params:
            params["max_bin"] = (1 << params.pop("log_max_bin")) - 1
        return params

    @classmethod
    def size(cls, config):
        num_leaves = int(round(config.get("num_leaves") or config["max_leaves"]))
        n_estimators = int(round(config["n_estimators"]))
        return (num_leaves * 3 + (num_leaves - 1) * 4 + 1.0) * n_estimators * 8

    def __init__(self, task="binary", **config):
        super().__init__(task, **config)
        if "verbose" not in self.params:
            self.params["verbose"] = -1
        if "regression" == task:
            from lightgbm import LGBMRegressor

            self.estimator_class = LGBMRegressor
        elif "rank" == task:
            from lightgbm import LGBMRanker

            self.estimator_class = LGBMRanker
        else:
            from lightgbm import LGBMClassifier

            self.estimator_class = LGBMClassifier
        self._time_per_iter = None
        self._train_size = 0

    def _preprocess(self, X):
        if (
            not isinstance(X, pd.DataFrame)
            and issparse(X)
            and np.issubdtype(X.dtype, np.integer)
        ):
            X = X.astype(float)
        elif isinstance(X, np.ndarray) and X.dtype.kind not in "buif":
            # numpy array is not of numeric dtype
            X = pd.DataFrame(X)
            for col in X.columns:
                if isinstance(X[col][0], str):
                    X[col] = X[col].astype("category").cat.codes
            X = X.to_numpy()
        return X

    def fit(self, X_train, y_train, budget=None, **kwargs):
        start_time = time.time()
        n_iter = self.params["n_estimators"]
        trained = False
        if (
            (not self._time_per_iter or abs(self._train_size - X_train.shape[0]) > 4)
            and budget is not None
            and n_iter > 1
        ):
            self.params["n_estimators"] = 1
            self._t1 = self._fit(X_train, y_train, **kwargs)
            if self._t1 >= budget or n_iter == 1:
                # self.params["n_estimators"] = n_iter
                return self._t1
            self.params["n_estimators"] = min(n_iter, 4)
            self._t2 = self._fit(X_train, y_train, **kwargs)
            self._time_per_iter = (
                (self._t2 - self._t1) / (self.params["n_estimators"] - 1)
                if self._t2 > self._t1
                else self._t1
                if self._t1
                else 0.001
            )
            self._train_size = X_train.shape[0]
            if self._t1 + self._t2 >= budget or n_iter == self.params["n_estimators"]:
                # self.params["n_estimators"] = n_iter
                return time.time() - start_time
            trained = True
        if budget is not None and n_iter > 1:
            max_iter = min(
                n_iter,
                int(
                    (budget - time.time() + start_time - self._t1) / self._time_per_iter
                    + 1
                ),
            )
            if trained and max_iter <= self.params["n_estimators"]:
                return time.time() - start_time
            self.params["n_estimators"] = max_iter
        if self.params["n_estimators"] > 0:
            self._fit(X_train, y_train, **kwargs)
        else:
            self.params["n_estimators"] = self._model.n_estimators
        train_time = time.time() - start_time
        return train_time


class XGBoostEstimator(SKLearnEstimator):
    """not using sklearn API, used for regression"""

    @classmethod
    def search_space(cls, data_size, **params):
        upper = min(32768, int(data_size))
        return {
            "n_estimators": {
                "domain": tune.lograndint(lower=4, upper=upper),
                "init_value": 4,
                "low_cost_init_value": 4,
            },
            "max_leaves": {
                "domain": tune.lograndint(lower=4, upper=upper),
                "init_value": 4,
                "low_cost_init_value": 4,
            },
            "min_child_weight": {
                "domain": tune.loguniform(lower=0.001, upper=128),
                "init_value": 1,
            },
            "learning_rate": {
                "domain": tune.loguniform(lower=1 / 1024, upper=1.0),
                "init_value": 0.1,
            },
            "subsample": {
                "domain": tune.uniform(lower=0.1, upper=1.0),
                "init_value": 1.0,
            },
            "colsample_bylevel": {
                "domain": tune.uniform(lower=0.01, upper=1.0),
                "init_value": 1.0,
            },
            "colsample_bytree": {
                "domain": tune.uniform(lower=0.01, upper=1.0),
                "init_value": 1.0,
            },
            "reg_alpha": {
                "domain": tune.loguniform(lower=1 / 1024, upper=1024),
                "init_value": 1 / 1024,
            },
            "reg_lambda": {
                "domain": tune.loguniform(lower=1 / 1024, upper=1024),
                "init_value": 1.0,
            },
        }

    @classmethod
    def size(cls, config):
        return LGBMEstimator.size(config)

    @classmethod
    def cost_relative2lgbm(cls):
        return 1.6

    def config2params(cls, config: dict) -> dict:
        params = super().config2params(config)
        params["max_depth"] = params.get("max_depth", 0)
        params["grow_policy"] = params.get("grow_policy", "lossguide")
        params["booster"] = params.get("booster", "gbtree")
        params["use_label_encoder"] = params.get("use_label_encoder", False)
        params["tree_method"] = params.get("tree_method", "hist")
        if "n_jobs" in config:
            params["nthread"] = params.pop("n_jobs")
        return params

    def __init__(
        self,
        task="regression",
        **config,
    ):
        super().__init__(task, **config)
        self.params["verbosity"] = 0

    def fit(self, X_train, y_train, budget=None, **kwargs):
        import xgboost as xgb

        start_time = time.time()
        if issparse(X_train):
            self.params["tree_method"] = "auto"
        else:
            X_train = self._preprocess(X_train)
        if "sample_weight" in kwargs:
            dtrain = xgb.DMatrix(X_train, label=y_train, weight=kwargs["sample_weight"])
        else:
            dtrain = xgb.DMatrix(X_train, label=y_train)

        objective = self.params.get("objective")
        if isinstance(objective, str):
            obj = None
        else:
            obj = objective
            if "objective" in self.params:
                del self.params["objective"]
        _n_estimators = self.params.pop("n_estimators")
        self._model = xgb.train(self.params, dtrain, _n_estimators, obj=obj)
        self.params["objective"] = objective
        self.params["n_estimators"] = _n_estimators
        del dtrain
        train_time = time.time() - start_time
        return train_time

    def predict(self, X_test):
        import xgboost as xgb

        if not issparse(X_test):
            X_test = self._preprocess(X_test)
        dtest = xgb.DMatrix(X_test)
        return super().predict(dtest)


class XGBoostSklearnEstimator(SKLearnEstimator, LGBMEstimator):
    """using sklearn API, used for classification"""

    @classmethod
    def search_space(cls, data_size, **params):
        return XGBoostEstimator.search_space(data_size)

    @classmethod
    def cost_relative2lgbm(cls):
        return XGBoostEstimator.cost_relative2lgbm()

    def config2params(cls, config: dict) -> dict:
        # TODO: test
        params = super(BaseEstimator).config2params(config)
        params["max_depth"] = 0
        params["grow_policy"] = params.get("grow_policy", "lossguide")
        params["booster"] = params.get("booster", "gbtree")
        params["use_label_encoder"] = params.get("use_label_encoder", False)
        params["tree_method"] = params.get("tree_method", "hist")
        return params

    def __init__(
        self,
        task="binary",
        **config,
    ):
        super().__init__(task, **config)
        del self.params["verbose"]
        self.params["verbosity"] = 0
        import xgboost as xgb

        self.estimator_class = xgb.XGBRegressor
        if "rank" == task:
            self.estimator_class = xgb.XGBRanker
        elif task in CLASSIFICATION:
            self.estimator_class = xgb.XGBClassifier

    def fit(self, X_train, y_train, budget=None, **kwargs):
        if issparse(X_train):
            self.params["tree_method"] = "auto"
        return super().fit(X_train, y_train, budget, **kwargs)


class RandomForestEstimator(SKLearnEstimator, LGBMEstimator):
    @classmethod
    def search_space(cls, data_size, task, **params):
        data_size = int(data_size)
        upper = min(2048, data_size)
        space = {
            "n_estimators": {
                "domain": tune.lograndint(lower=4, upper=upper),
                "init_value": 4,
                "low_cost_init_value": 4,
            },
            "max_features": {
                "domain": tune.loguniform(lower=0.1, upper=1.0),
                "init_value": 1.0,
            },
            "max_leaves": {
                "domain": tune.lograndint(lower=4, upper=min(32768, data_size)),
                "init_value": 4,
                "low_cost_init_value": 4,
            },
        }
        if task in CLASSIFICATION:
            space["criterion"] = {
                "domain": tune.choice(["gini", "entropy"]),
                # 'init_value': 'gini',
            }
        return space

    @classmethod
    def cost_relative2lgbm(cls):
        return 2.0

    def config2params(cls, config: dict) -> dict:
        params = super(BaseEstimator).config2params(config)
        if "max_leaves" in params:
            params["max_leaf_nodes"] = params.get(
                "max_leaf_nodes", params.pop("max_leaves")
            )
        return params

    def __init__(
        self,
        task="binary",
        **params,
    ):
        super().__init__(task, **params)
        self.params["verbose"] = 0
        self.estimator_class = RandomForestRegressor
        if task in CLASSIFICATION:
            self.estimator_class = RandomForestClassifier


class ExtraTreeEstimator(RandomForestEstimator):
    @classmethod
    def cost_relative2lgbm(cls):
        return 1.9

    def __init__(self, task="binary", **params):
        super().__init__(task, **params)
        if "regression" in task:
            self.estimator_class = ExtraTreesRegressor
        else:
            self.estimator_class = ExtraTreesClassifier


class LRL1Classifier(SKLearnEstimator):
    @classmethod
    def search_space(cls, **params):
        return {
            "C": {
                "domain": tune.loguniform(lower=0.03125, upper=32768.0),
                "init_value": 1.0,
            },
        }

    @classmethod
    def cost_relative2lgbm(cls):
        return 160

    def config2params(cls, config: dict) -> dict:
        params = super().config2params(config)
        params["tol"] = params.get("tol", 0.0001)
        params["solver"] = params.get("solver", "saga")
        params["penalty"] = params.get("penalty", "l1")
        return params

    def __init__(self, task="binary", **config):
        super().__init__(task, **config)
        assert task in CLASSIFICATION, "LogisticRegression for classification task only"
        self.estimator_class = LogisticRegression


class LRL2Classifier(SKLearnEstimator):
    @classmethod
    def search_space(cls, **params):
        return LRL1Classifier.search_space(**params)

    @classmethod
    def cost_relative2lgbm(cls):
        return 25

    def config2params(cls, config: dict) -> dict:
        params = super().config2params(config)
        params["tol"] = params.get("tol", 0.0001)
        params["solver"] = params.get("solver", "lbfgs")
        params["penalty"] = params.get("penalty", "l2")
        return params

    def __init__(self, task="binary", **config):
        super().__init__(task, **config)
        assert task in CLASSIFICATION, "LogisticRegression for classification task only"
        self.estimator_class = LogisticRegression


class CatBoostEstimator(BaseEstimator):
    _time_per_iter = None
    _train_size = 0

    @classmethod
    def search_space(cls, data_size, **params):
        upper = max(min(round(1500000 / data_size), 150), 12)
        return {
            "early_stopping_rounds": {
                "domain": tune.lograndint(lower=10, upper=upper),
                "init_value": 10,
                "low_cost_init_value": 10,
            },
            "learning_rate": {
                "domain": tune.loguniform(lower=0.005, upper=0.2),
                "init_value": 0.1,
            },
            "n_estimators": {
                "domain": 8192,
                "init_value": 8192,
            },
        }

    @classmethod
    def size(cls, config):
        n_estimators = config.get("n_estimators", 8192)
        max_leaves = 64
        return (max_leaves * 3 + (max_leaves - 1) * 4 + 1.0) * n_estimators * 8

    @classmethod
    def cost_relative2lgbm(cls):
        return 15

    @classmethod
    def init(cls):
        CatBoostEstimator._time_per_iter = None
        CatBoostEstimator._train_size = 0

    def _preprocess(self, X):
        if isinstance(X, pd.DataFrame):
            cat_columns = X.select_dtypes(include=["category"]).columns
            if not cat_columns.empty:
                X = X.copy()
                X[cat_columns] = X[cat_columns].apply(
                    lambda x: x.cat.rename_categories(
                        [
                            str(c) if isinstance(c, float) else c
                            for c in x.cat.categories
                        ]
                    )
                )
        elif isinstance(X, np.ndarray) and X.dtype.kind not in "buif":
            # numpy array is not of numeric dtype
            X = pd.DataFrame(X)
            for col in X.columns:
                if isinstance(X[col][0], str):
                    X[col] = X[col].astype("category").cat.codes
            X = X.to_numpy()
        return X

    def config2params(cls, config: dict) -> dict:
        params = super().config2params(config)
        params["n_estimators"] = params.get("n_estimators", 8192)
        if "n_jobs" in params:
            params["thread_count"] = params.pop("n_jobs")
        return params

    def __init__(
        self,
        task="binary",
        **config,
    ):
        super().__init__(task, **config)
        self.params.update(
            {
                "verbose": config.get("verbose", False),
                "random_seed": config.get("random_seed", 10242048),
            }
        )
        from catboost import CatBoostRegressor

        self.estimator_class = CatBoostRegressor
        if task in CLASSIFICATION:
            from catboost import CatBoostClassifier

            self.estimator_class = CatBoostClassifier

    def fit(self, X_train, y_train, budget=None, **kwargs):
        import shutil

        start_time = time.time()
        train_dir = f"catboost_{str(start_time)}"
        n_iter = self.params["n_estimators"]
        X_train = self._preprocess(X_train)
        if isinstance(X_train, pd.DataFrame):
            cat_features = list(X_train.select_dtypes(include="category").columns)
        else:
            cat_features = []
        # from catboost import CatBoostError
        # try:
        trained = False
        if (
            (
                not CatBoostEstimator._time_per_iter
                or abs(CatBoostEstimator._train_size - len(y_train)) > 4
            )
            and budget
            and n_iter > 4
        ):
            # measure the time per iteration
            self.params["n_estimators"] = 1
            CatBoostEstimator._smallmodel = self.estimator_class(
                train_dir=train_dir, **self.params
            )
            CatBoostEstimator._smallmodel.fit(
                X_train, y_train, cat_features=cat_features, **kwargs
            )
            CatBoostEstimator._t1 = time.time() - start_time
            if CatBoostEstimator._t1 >= budget or n_iter == 1:
                # self.params["n_estimators"] = n_iter
                self._model = CatBoostEstimator._smallmodel
                shutil.rmtree(train_dir, ignore_errors=True)
                return CatBoostEstimator._t1
            self.params["n_estimators"] = min(n_iter, 4)
            CatBoostEstimator._smallmodel = self.estimator_class(
                train_dir=train_dir, **self.params
            )
            CatBoostEstimator._smallmodel.fit(
                X_train, y_train, cat_features=cat_features, **kwargs
            )
            CatBoostEstimator._time_per_iter = (
                time.time() - start_time - CatBoostEstimator._t1
            ) / (self.params["n_estimators"] - 1)
            if CatBoostEstimator._time_per_iter <= 0:
                CatBoostEstimator._time_per_iter = CatBoostEstimator._t1
            CatBoostEstimator._train_size = len(y_train)
            if (
                time.time() - start_time >= budget
                or n_iter == self.params["n_estimators"]
            ):
                # self.params["n_estimators"] = n_iter
                self._model = CatBoostEstimator._smallmodel
                shutil.rmtree(train_dir, ignore_errors=True)
                return time.time() - start_time
            trained = True
        if budget and n_iter > 4:
            train_times = 1
            max_iter = min(
                n_iter,
                int(
                    (budget - time.time() + start_time - CatBoostEstimator._t1)
                    / train_times
                    / CatBoostEstimator._time_per_iter
                    + 1
                ),
            )
            self._model = CatBoostEstimator._smallmodel
            if trained and max_iter <= self.params["n_estimators"]:
                return time.time() - start_time
            self.params["n_estimators"] = max_iter
        if self.params["n_estimators"] > 0:
            n = max(int(len(y_train) * 0.9), len(y_train) - 1000)
            X_tr, y_tr = X_train[:n], y_train[:n]
            if "sample_weight" in kwargs:
                weight = kwargs["sample_weight"]
                if weight is not None:
                    kwargs["sample_weight"] = weight[:n]
            else:
                weight = None
            from catboost import Pool

            model = self.estimator_class(train_dir=train_dir, **self.params)
            model.fit(
                X_tr,
                y_tr,
                cat_features=cat_features,
                eval_set=Pool(
                    data=X_train[n:], label=y_train[n:], cat_features=cat_features
                ),
                **kwargs,
            )  # model.get_best_iteration()
            shutil.rmtree(train_dir, ignore_errors=True)
            if weight is not None:
                kwargs["sample_weight"] = weight
            self._model = model
        else:
            self.params["n_estimators"] = self._model.tree_count_
        # except CatBoostError:
        #     self._model = None
        train_time = time.time() - start_time
        return train_time


class KNeighborsEstimator(BaseEstimator):
    @classmethod
    def search_space(cls, data_size, **params):
        upper = min(512, int(data_size / 2))
        return {
            "n_neighbors": {
                "domain": tune.lograndint(lower=1, upper=upper),
                "init_value": 5,
                "low_cost_init_value": 1,
            },
        }

    @classmethod
    def cost_relative2lgbm(cls):
        return 30

    def config2params(cls, config: dict) -> dict:
        params = super().config2params(config)
        params["weights"] = params.get("weights", "distance")
        return params

    def __init__(self, task="binary", **config):
        super().__init__(task, **config)
        if task in CLASSIFICATION:
            from sklearn.neighbors import KNeighborsClassifier

            self.estimator_class = KNeighborsClassifier
        else:
            from sklearn.neighbors import KNeighborsRegressor

            self.estimator_class = KNeighborsRegressor

    def _preprocess(self, X):
        if isinstance(X, pd.DataFrame):
            cat_columns = X.select_dtypes(["category"]).columns
            if X.shape[1] == len(cat_columns):
                raise ValueError("kneighbor requires at least one numeric feature")
            X = X.drop(cat_columns, axis=1)
        elif isinstance(X, np.ndarray) and X.dtype.kind not in "buif":
            # drop categocial columns if any
            X = pd.DataFrame(X)
            cat_columns = []
            for col in X.columns:
                if isinstance(X[col][0], str):
                    cat_columns.append(col)
            X = X.drop(cat_columns, axis=1)
            X = X.to_numpy()
        return X


class Prophet(SKLearnEstimator):
    @classmethod
    def search_space(cls, **params):
        space = {
            "changepoint_prior_scale": {
                "domain": tune.loguniform(lower=0.001, upper=0.05),
                "init_value": 0.05,
                "low_cost_init_value": 0.001,
            },
            "seasonality_prior_scale": {
                "domain": tune.loguniform(lower=0.01, upper=10),
                "init_value": 10,
            },
            "holidays_prior_scale": {
                "domain": tune.loguniform(lower=0.01, upper=10),
                "init_value": 10,
            },
            "seasonality_mode": {
                "domain": tune.choice(["additive", "multiplicative"]),
                "init_value": "multiplicative",
            },
        }
        return space

    def __init__(self, task=TS_FORECAST, n_jobs=1, resources_per_trial=None, **params):
        super().__init__(task, **params)

    def _join(self, X_train, y_train):
        assert TS_TIMESTAMP_COL in X_train, (
            "Dataframe for training ts_forecast model must have column"
            f' "{TS_TIMESTAMP_COL}" with the dates in X_train.'
        )
        y_train = pd.DataFrame(y_train, columns=[TS_VALUE_COL])
        train_df = X_train.join(y_train)
        return train_df

    def fit(self, X_train, y_train, budget=None, **kwargs):
        from prophet import Prophet

        current_time = time.time()
        train_df = self._join(X_train, y_train)
        train_df = self._preprocess(train_df)
        cols = list(train_df)
        cols.remove(TS_TIMESTAMP_COL)
        cols.remove(TS_VALUE_COL)
        model = Prophet(**self.params)
        for regressor in cols:
            model.add_regressor(regressor)
        model.fit(train_df)
        train_time = time.time() - current_time
        self._model = model
        return train_time

    def predict(self, X_test):
        if isinstance(X_test, int):
            raise ValueError(
                "predict() with steps is only supported for arima/sarimax."
                " For Prophet, pass a dataframe with the first column containing"
                " the timestamp values."
            )
        if self._model is not None:
            X_test = self._preprocess(X_test)
            forecast = self._model.predict(X_test)
            return forecast["yhat"]
        else:
            logger.warning(
                "Estimator is not fit yet. Please run fit() before predict()."
            )
            return np.ones(X_test.shape[0])


class ARIMA(Prophet):
    @classmethod
    def search_space(cls, **params):
        space = {
            "p": {
                "domain": tune.quniform(lower=0, upper=10, q=1),
                "init_value": 2,
                "low_cost_init_value": 0,
            },
            "d": {
                "domain": tune.quniform(lower=0, upper=10, q=1),
                "init_value": 2,
                "low_cost_init_value": 0,
            },
            "q": {
                "domain": tune.quniform(lower=0, upper=10, q=1),
                "init_value": 1,
                "low_cost_init_value": 0,
            },
        }
        return space

    def _join(self, X_train, y_train):
        train_df = super()._join(X_train, y_train)
        train_df.index = pd.to_datetime(train_df[TS_TIMESTAMP_COL])
        train_df = train_df.drop(TS_TIMESTAMP_COL, axis=1)
        return train_df

    def fit(self, X_train, y_train, budget=None, **kwargs):
        import warnings

        warnings.filterwarnings("ignore")
        from statsmodels.tsa.arima.model import ARIMA as ARIMA_estimator

        current_time = time.time()
        train_df = self._join(X_train, y_train)
        train_df = self._preprocess(train_df)
        cols = list(train_df)
        cols.remove(TS_VALUE_COL)
        regressors = cols
        if regressors:
            model = ARIMA_estimator(
                train_df[[TS_VALUE_COL]],
                exog=train_df[regressors],
                order=(self.params["p"], self.params["d"], self.params["q"]),
                enforce_stationarity=False,
                enforce_invertibility=False,
            )
        else:
            model = ARIMA_estimator(
                train_df,
                order=(self.params["p"], self.params["d"], self.params["q"]),
                enforce_stationarity=False,
                enforce_invertibility=False,
            )
        model = model.fit()
        train_time = time.time() - current_time
        self._model = model
        return train_time

    def predict(self, X_test):
        if self._model is not None:
            if isinstance(X_test, int):
                forecast = self._model.forecast(steps=X_test)
            elif isinstance(X_test, pd.DataFrame):
                first_col = X_test.pop(TS_TIMESTAMP_COL)
                X_test.insert(0, TS_TIMESTAMP_COL, first_col)
                start = X_test.iloc[0, 0]
                end = X_test.iloc[-1, 0]
                if len(X_test.columns) > 1:
                    regressors = list(X_test)
                    regressors.remove(TS_TIMESTAMP_COL)
                    X_test = self._preprocess(X_test)
                    forecast = self._model.predict(
                        start=start, end=end, exog=X_test[regressors]
                    )
                else:
                    forecast = self._model.predict(start=start, end=end)
            else:
                raise ValueError(
                    "X_test needs to be either a pd.Dataframe with dates as the first column"
                    " or an int number of periods for predict()."
                )
            return forecast
        else:
            return np.ones(X_test if isinstance(X_test, int) else X_test.shape[0])


class SARIMAX(ARIMA):
    @classmethod
    def search_space(cls, **params):
        space = {
            "p": {
                "domain": tune.quniform(lower=0, upper=10, q=1),
                "init_value": 2,
                "low_cost_init_value": 0,
            },
            "d": {
                "domain": tune.quniform(lower=0, upper=10, q=1),
                "init_value": 2,
                "low_cost_init_value": 0,
            },
            "q": {
                "domain": tune.quniform(lower=0, upper=10, q=1),
                "init_value": 1,
                "low_cost_init_value": 0,
            },
            "P": {
                "domain": tune.quniform(lower=0, upper=10, q=1),
                "init_value": 1,
                "low_cost_init_value": 0,
            },
            "D": {
                "domain": tune.quniform(lower=0, upper=10, q=1),
                "init_value": 1,
                "low_cost_init_value": 0,
            },
            "Q": {
                "domain": tune.quniform(lower=0, upper=10, q=1),
                "init_value": 1,
                "low_cost_init_value": 0,
            },
            "s": {
                "domain": tune.choice([1, 4, 6, 12]),
                "init_value": 12,
            },
        }
        return space

    def fit(self, X_train, y_train, budget=None, **kwargs):
        import warnings

        warnings.filterwarnings("ignore")
        from statsmodels.tsa.statespace.sarimax import SARIMAX as SARIMAX_estimator

        current_time = time.time()
        train_df = self._join(X_train, y_train)
        train_df = self._preprocess(train_df)
        regressors = list(train_df)
        regressors.remove(TS_VALUE_COL)
        if regressors:
            model = SARIMAX_estimator(
                train_df[[TS_VALUE_COL]],
                exog=train_df[regressors],
                order=(self.params["p"], self.params["d"], self.params["q"]),
                seasonality_order=(
                    self.params["P"],
                    self.params["D"],
                    self.params["Q"],
                    self.params["s"],
                ),
                enforce_stationarity=False,
                enforce_invertibility=False,
            )
        else:
            model = SARIMAX_estimator(
                train_df,
                order=(self.params["p"], self.params["d"], self.params["q"]),
                seasonality_order=(
                    self.params["P"],
                    self.params["D"],
                    self.params["Q"],
                    self.params["s"],
                ),
                enforce_stationarity=False,
                enforce_invertibility=False,
            )
        model = model.fit()
        train_time = time.time() - current_time
        self._model = model
        return train_time
