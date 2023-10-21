from __future__ import annotations

import os
import random
import re
from typing import List, Union, Optional, Callable

import numpy as np
import torch
from cytoolz import merge_with
from torch import nn, Tensor
from torch.nn.utils.rnn import pad_sequence
from transformers import T5ForConditionalGeneration, Adafactor, T5TokenizerFast
from sentence_transformers import SentenceTransformer
from sentence_transformers import util

from src import ExperimentConfig
from src.data.templates import Task

# sim_model = SentenceTransformer('all-MiniLM-L6-v2', device="cuda:0")


class UserEmbeds(nn.Module):

    def __init__(self, n_users, dim_model):
        super().__init__()

        self.emb_layer = nn.Embedding(n_users, dim_model)
        torch.nn.init.xavier_uniform_(self.emb_layer.weight)

    def __call__(self, user_idx):

        x = self.emb_layer(user_idx)

        # we dropout an entire column (neuron)
        x = x.permute(1, 0)
        x = nn.functional.dropout1d(x, p=0.6, training=self.training)
        x = x.permute(1, 0)

        x = nn.functional.leaky_relu(x)

        return x


class T5FineTuned(T5ForConditionalGeneration):

    def __init__(self,
                 config,
                 n_users: int,
                 training_tasks: List[Task],
                 all_unique_labels: np.ndarray[str],
                 eval_task: Task = None,
                 device: str = "cpu"):

        super().__init__(config)

        self.tokenizer = T5TokenizerFast.from_pretrained(config.name_or_path)

        self.training_tasks = training_tasks
        self.eval_task = eval_task

        self.n_users = n_users
        self.all_unique_labels = all_unique_labels
        # self.encoded_all_labels = sim_model.encode(list(self.all_unique_labels),
        #                                            convert_to_tensor=True,
        #                                            show_progress_bar=True)

        # Set maximum 512 whole words in a source text
        self.user_embeddings = UserEmbeds(n_users, self.config.d_model)

        self.to(device)

    def get_suggested_optimizer(self):
        return Adafactor(
            list(self.parameters()),
            lr=1e-3,
            eps=(1e-30, 1e-3),
            clip_threshold=1.0,
            decay_rate=-0.8,
            beta1=None,
            weight_decay=0.01,
            relative_step=False,
            scale_parameter=False,
            warmup_init=False
        )

    def set_eval_task(self, eval_task: Task):
        self.eval_task = eval_task

    def tokenize(self, sample):

        assert len(sample["user_id"]) == 1, "set batch_size to map fn to 1"

        sample = {k: v[0] for k, v in sample.items()}

        task = random.choice(self.training_tasks) if self.training else self.eval_task

        # give all info that we have about the sample to the task randomly sampled to generate
        # input prompt and target text. Each task may have mandatory arguments, if they are missing
        # an assertion error will be raised
        templates_list = task(**sample)

        encoded_sequence_list = []
        for input_text, target_text in templates_list:

            encoded_sequence = self.tokenizer(text=input_text, text_target=target_text, truncation=True)

            # get word ids from t5 tokenizer fast
            whole_word_ids = np.array(encoded_sequence.encodings[0].word_ids)
            special_token_mask = np.array(encoded_sequence.encodings[0].special_tokens_mask).astype(bool)

            # we set -1 to all special tokens (to substitute None, which is the value set by default)
            whole_word_ids[~special_token_mask] += 1
            whole_word_ids[special_token_mask] = self.tokenizer.pad_token_id

            encoded_sequence["user_idx"] = int(re.search(r"\d+", sample["user_id"]).group())
            encoded_sequence["whole_word_ids"] = whole_word_ids.tolist()
            encoded_sequence["target_item"] = sample["target_item"]

            encoded_sequence_list.append(encoded_sequence)

        return merge_with(list, *encoded_sequence_list)

    def prepare_input(self, batch):
        input_dict = {}

        input_ids = pad_sequence(batch["input_ids"], batch_first=True, padding_value=self.tokenizer.pad_token_id)
        attention_mask = pad_sequence(batch["attention_mask"],
                                      batch_first=True,
                                      padding_value=self.tokenizer.pad_token_id)
        whole_word_ids = pad_sequence(batch["whole_word_ids"],
                                      batch_first=True,
                                      padding_value=self.tokenizer.pad_token_id)

        input_dict["user_idx"] = batch["user_idx"].to(self.device)
        input_dict["input_ids"] = input_ids.to(self.device)
        input_dict["attention_mask"] = attention_mask.to(self.device)
        input_dict["whole_word_ids"] = whole_word_ids.to(self.device)

        if "labels" in batch:
            lm_labels = pad_sequence(batch["labels"], batch_first=True, padding_value=self.tokenizer.pad_token_id)
            lm_labels[lm_labels == self.tokenizer.pad_token_id] = -100

            input_dict["labels"] = lm_labels.to(self.device)

        if not self.training:
            input_dict["target_item"] = batch["target_item"]

        return input_dict

    def _inject_personalization(self, token_inputs_embeds: Tensor, user_idxs: Tensor):

        # whole_word_embeds = self.whole_word_embeddings(whole_word_ids)
        # # whole_word_embeds = self.relu(whole_word_embeds)
        # assert whole_word_embeds.shape[-1] == token_inputs_embeds.shape[-1]
        # inputs_embeds = token_inputs_embeds + whole_word_embeds

        # user idxs start from 1, TO IMPROVE!
        user_embeds = self.user_embeddings(user_idxs - 1).unsqueeze(dim=1)
        # whole_word_embeds = self.relu(whole_word_embeds)
        inputs_embeds = token_inputs_embeds + user_embeds

        return inputs_embeds

    def train_step(self, batch):

        inputs_embeds = self.shared(batch["input_ids"])  # embedding step - add HERE

        if "train" in ExperimentConfig.inject_personalization:
            inputs_embeds = self._inject_personalization(inputs_embeds, batch["user_idx"])

        output = self(inputs_embeds=inputs_embeds,
                      attention_mask=batch["attention_mask"],
                      labels=batch["labels"])

        return output.loss

    @torch.no_grad()
    def valid_step(self, batch):

        if self.eval_task is None:
            raise ValueError("Model can't perform valid_step since no eval_task is set! "
                             "Pass it when initializing the model or with `set_eval_task()`")

        num_return_sequences = 10
        max_new_tokens = 50
        num_beams = 30
        no_repeat_ngram_size = 0
        early_stopping = True

        target_text = batch.pop("target_item")

        inputs_embeds = self.shared(batch["input_ids"])
        if "eval" in ExperimentConfig.inject_personalization:
            inputs_embeds = self._inject_personalization(inputs_embeds, batch["user_idx"])

        output = self(inputs_embeds=inputs_embeds,
                      attention_mask=batch["attention_mask"],
                      labels=batch["labels"])

        beam_outputs = self.generate(
            inputs_embeds=inputs_embeds,
            attention_mask=batch["attention_mask"],
            num_return_sequences=num_return_sequences,
            max_new_tokens=max_new_tokens,
            num_beams=num_beams,
            no_repeat_ngram_size=no_repeat_ngram_size,
            early_stopping=early_stopping
        )

        generated_sents = self.tokenizer.batch_decode(beam_outputs, skip_special_tokens=True)
        # encoded_preds = sim_model.encode(generated_sents, show_progress_bar=False, convert_to_tensor=True)

        # sim = util.cos_sim(encoded_preds, self.encoded_all_labels).cpu()
        # mapped_predictions = self.all_unique_labels[sim.argmax(axis=1)]

        # mapped predictions is 1d. What we want is to have an array of shape (batch_size x num_return sequences)
        # mapped_predictions = mapped_predictions.reshape((len(target_text), num_return_sequences))

        mapped_predictions = np.array(generated_sents).reshape((len(target_text), num_return_sequences))
        val_loss = output.loss

        return mapped_predictions, target_text, val_loss

    def save_pretrained(
        self,
        save_directory: Union[str, os.PathLike],
        is_main_process: bool = True,
        state_dict: Optional[dict] = None,
        save_function: Callable = torch.save,
        push_to_hub: bool = False,
        max_shard_size: Union[int, str] = "10GB",
        safe_serialization: bool = False,
        variant: Optional[str] = None,
        token: Optional[Union[str, bool]] = None,
        save_peft_format: bool = True,
        **kwargs,
    ):

        super().save_pretrained(save_directory=save_directory,
                                is_main_process=is_main_process,
                                state_dict=state_dict,
                                save_function=save_function,
                                push_to_hub=push_to_hub,
                                max_shard_size=max_shard_size,
                                safe_serialization=safe_serialization,
                                variant=variant,
                                token=token,
                                save_peft_format=save_peft_format,
                                **kwargs)

        self.tokenizer.save_pretrained(save_directory=save_directory)

    def train(self, mode: bool = True):

        if mode is True:
            Task.train()
        else:
            Task.eval()

        return super().train(mode)

    def eval(self):

        Task.eval()

        return super().eval()
