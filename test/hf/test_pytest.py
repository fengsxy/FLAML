'''Require: pip install torch transformers datasets wandb flaml[blendsearch,ray]
'''
#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
import os
import shutil

from flaml.nlp import AutoTransformers
from flaml.nlp import AzureUtils, JobID
from flaml.nlp.result_analysis.wandb_utils import WandbUtils
from flaml.nlp.utils import load_console_args

global azure_log_path
global azure_key

def get_preparedata_setting(jobid_config, wandb_utils):
    preparedata_setting = {
        "server_name": "tmdev",
        "data_root_path": "data/",
        "max_seq_length": 128,
        "jobid_config": jobid_config,
        "wandb_utils": wandb_utils,
        "resplit_portion": {"source": ["train", "validation"], "train": [0, 0.8], "validation": [0.8, 0.9], "test": [0.9, 1.0]}
        }
    return preparedata_setting

def get_autohf_settings():
    autohf_settings = {"resources_per_trial": {"gpu": 1, "cpu": 1},
                       "num_samples": 1,
                       "time_budget": 100000,
                       "ckpt_per_epoch": 1,
                      }
    return autohf_settings

def test_hpo():
    jobid_config = JobID()
    jobid_config.set_unittest_config()
    autohf = AutoTransformers()
    wandb_utils = WandbUtils(is_wandb_on=False, jobid_config=jobid_config)

    try:
        preparedata_setting = get_preparedata_setting(jobid_config, wandb_utils)
        autohf.prepare_data(**preparedata_setting)

        autohf_settings = get_autohf_settings()
        validation_metric, analysis = autohf.fit(**autohf_settings,)

        predictions, test_metric = autohf.predict()
        if test_metric:
            validation_metric.update({"test": test_metric})

    except AssertionError as err:
        pass

if __name__ == "__main__":
    test_hpo()