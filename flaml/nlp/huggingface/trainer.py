import os

try:
    from transformers import Trainer as TFTrainer
except ImportError:
    TFTrainer = object


class TrainerForAutoTransformers(TFTrainer):
    def evaluate(self, eval_dataset=None):
        """
        Overriding transformers.Trainer.evaluate by saving state with save_state

        Args:
            eval_dataset:
                the dataset to be evaluated
        """
        from ray import tune

        eval_dataloader = self.get_eval_dataloader(eval_dataset)
        output = self.prediction_loop(eval_dataloader, description="Evaluation")
        self.log(output.metrics)

        ckpt_dir = self.save_state()

        for key in list(output.metrics.keys()):
            if key.startswith("eval_"):
                output.metrics[key[5:]] = output.metrics.pop(key)

        if not hasattr(self, "ckpt_to_metric"):
            self.ckpt_to_metric = {}
        self.ckpt_to_metric[ckpt_dir] = output.metrics

    @staticmethod
    def tune_report(mode="holdout", output_metrics=None):
        from ray import tune
        import numpy as np

        if mode == "holdout":
            tune.report(**output_metrics)
        else:
            avg_metrics = {}
            for each_output_metrics in output_metrics:
                for key, val in each_output_metrics.items():
                    if key.startswith("eval_"):
                        avg_metrics.setdefault(key[5:], [])
                        avg_metrics[key[5:]].append(val)
            for key in list(avg_metrics.keys()):
                avg_metrics[key] = np.mean(avg_metrics[key])
            tune.report(**avg_metrics)

    def save_state(self):
        """
        Overriding transformers.Trainer.save_state. It is only through saving
        the states can best_trial.get_best_checkpoint return a non-empty value.
        """
        import torch
        from transformers.trainer_utils import PREFIX_CHECKPOINT_DIR
        from ray import tune

        with tune.checkpoint_dir(step=self.state.global_step) as checkpoint_dir:
            self.args.output_dir = checkpoint_dir
            # This is the directory name that Huggingface requires.
            output_dir = os.path.join(
                self.args.output_dir,
                f"{PREFIX_CHECKPOINT_DIR}-{self.state.global_step}",
            )
            self.save_model(output_dir)
            torch.save(
                self.optimizer.state_dict(), os.path.join(output_dir, "optimizer.pt")
            )
            torch.save(
                self.lr_scheduler.state_dict(), os.path.join(output_dir, "scheduler.pt")
            )
            return output_dir

    @staticmethod
    def convert_num_train_epochs_to_max_steps(
        num_train_epochs: int,
        num_train_examples: int,
        per_device_train_batch_size: int,
        device_count: int,
    ):
        return int(
            num_train_epochs
            * num_train_examples
            / per_device_train_batch_size
            / device_count
        )

    @staticmethod
    def convert_max_steps_to_num_train_epochs(
        max_steps: int,
        num_train_examples: int,
        per_device_train_batch_size: int,
        device_count: int,
    ):
        return (
            float(max_steps * per_device_train_batch_size * device_count)
            / num_train_examples
        )

    @staticmethod
    def convert_warmup_ratio_to_warmup_steps(
        warmup_ratio,
        max_steps=None,
        num_train_epochs=None,
        num_train_examples=None,
        per_device_train_batch_size=None,
        device_count=None,
    ):
        if max_steps:
            return int(warmup_ratio * max_steps)
        max_steps = TrainerForAutoTransformers.convert_num_train_epochs_to_max_steps(
            num_train_epochs,
            num_train_examples,
            per_device_train_batch_size,
            device_count,
        )
        return int(warmup_ratio * max_steps)

    @staticmethod
    def convert_warmup_steps_to_warmup_ratio(
        warmup_steps: int,
        num_train_epochs: int,
        num_train_examples: int,
        per_device_train_batch_size: int,
        device_count: int,
    ):
        max_steps = TrainerForAutoTransformers.convert_num_train_epochs_to_max_steps(
            num_train_epochs,
            num_train_examples,
            per_device_train_batch_size,
            device_count,
        )
        return float(warmup_steps / max_steps)
