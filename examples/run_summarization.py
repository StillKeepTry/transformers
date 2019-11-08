# coding=utf-8
# Copyright 2019 The HuggingFace Inc. team.
# Copyright (c) 2019 The HuggingFace Inc.  All rights reserved.
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
""" Finetuning seq2seq models for sequence generation."""

import argparse
import copy
import functools
import logging
import os
import random
import sys

import numpy as np
from tqdm import tqdm, trange
import torch
from torch.optim import Adam
from torch.utils.data import DataLoader, RandomSampler, SequentialSampler

from transformers import (
    AutoTokenizer,
    BertForMaskedLM,
    BertConfig,
    PreTrainedEncoderDecoder,
    Model2Model,
)

from transformers.generate import BeamSearch

from utils_summarization import (
    CNNDailyMailDataset,
    encode_for_summarization,
    fit_to_block_size,
    build_lm_labels,
    build_mask,
    compute_token_type_ids,
)

logger = logging.getLogger(__name__)
logging.basicConfig(stream=sys.stdout, level=logging.INFO)


def set_seed(args):
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)


# ------------
# Load dataset
# ------------


def load_and_cache_examples(args, tokenizer):
    dataset = CNNDailyMailDataset(args.data_dir)
    return dataset


def collate(data, tokenizer, block_size):
    """ List of tuple as an input. """
    # remove the files with empty an story/summary, encode and fit to block
    data = filter(lambda x: not (len(x[0]) == 0 or len(x[1]) == 0), data)
    data = [encode_for_summarization(story, summary, tokenizer) for story, summary in data]
    data = [
        (
            fit_to_block_size(story, block_size, tokenizer.pad_token_id),
            fit_to_block_size(summary, block_size, tokenizer.pad_token_id),
        )
        for story, summary in data
    ]

    stories = torch.tensor([story for story, summary in data])
    summaries = torch.tensor([summary for story, summary in data])
    encoder_token_type_ids = compute_token_type_ids(stories, tokenizer.cls_token_id)
    encoder_mask = build_mask(stories, tokenizer.pad_token_id)
    decoder_mask = build_mask(summaries, tokenizer.pad_token_id)
    lm_labels = build_lm_labels(summaries, tokenizer.pad_token_id)

    return (
        stories,
        summaries,
        encoder_token_type_ids,
        encoder_mask,
        decoder_mask,
        lm_labels,
    )


# ----------
# Optimizers
# ----------


class BertSumOptimizer(object):
    """ Specific optimizer for BertSum.

    As described in [1], the authors fine-tune BertSum for abstractive
    summarization using two Adam Optimizers with different warm-up steps and
    learning rate. They also use a custom learning rate scheduler.

    [1] Liu, Yang, and Mirella Lapata. "Text summarization with pretrained encoders."
        arXiv preprint arXiv:1908.08345 (2019).
    """

    def __init__(self, model, lr, warmup_steps, beta_1=0.99, beta_2=0.999, eps=1e-8):
        self.encoder = model.encoder
        self.decoder = model.decoder
        self.lr = lr
        self.warmup_steps = warmup_steps

        self.optimizers = {
            "encoder": Adam(
                model.encoder.parameters(),
                lr=lr["encoder"],
                betas=(beta_1, beta_2),
                eps=eps,
            ),
            "decoder": Adam(
                model.decoder.parameters(),
                lr=lr["decoder"],
                betas=(beta_1, beta_2),
                eps=eps,
            ),
        }

        self._step = 0

    def _update_rate(self, stack):
        return self.lr[stack] * min(
            self._step ** (-0.5), self._step * self.warmup_steps[stack] ** (-0.5)
        )

    def zero_grad(self):
        self.optimizer_decoder.zero_grad()
        self.optimizer_encoder.zero_grad()

    def step(self):
        self._step += 1
        for stack, optimizer in self.optimizers.items():
            new_rate = self._update_rate(stack)
            for param_group in optimizer.param_groups:
                param_group["lr"] = new_rate
            optimizer.step()


# ------------
# Train
# ------------


def train(args, model, tokenizer):
    """ Fine-tune the pretrained model on the corpus. """
    set_seed(args)

    # Load the data
    args.train_batch_size = args.per_gpu_train_batch_size * max(1, args.n_gpu)
    train_dataset = load_and_cache_examples(args, tokenizer)
    train_sampler = RandomSampler(train_dataset)
    model_collate_fn = functools.partial(collate, tokenizer=tokenizer, block_size=512)
    train_dataloader = DataLoader(
        train_dataset,
        sampler=train_sampler,
        batch_size=args.train_batch_size,
        collate_fn=model_collate_fn,
    )

    # Training schedule
    if args.max_steps > 0:
        t_total = args.max_steps
        args.num_train_epochs = t_total // (
            len(train_dataloader) // args.gradient_accumulation_steps + 1
        )
    else:
        t_total = (
            len(train_dataloader)
            // args.gradient_accumulation_steps
            * args.num_train_epochs
        )

    # Prepare the optimizer
    lr = {"encoder": 0.002, "decoder": 0.2}
    warmup_steps = {"encoder": 20000, "decoder": 10000}
    optimizer = BertSumOptimizer(model, lr, warmup_steps)

    # Train
    logger.info("***** Running training *****")
    logger.info("  Num examples = %d", len(train_dataset))
    logger.info("  Num Epochs = %d", args.num_train_epochs)
    logger.info("  Instantaneous batch size per GPU = %d", args.per_gpu_train_batch_size)
    logger.info(
        "  Total train batch size (w. parallel, distributed & accumulation) = %d",
        args.train_batch_size * args.gradient_accumulation_steps
        # * (torch.distributed.get_world_size() if args.local_rank != -1 else 1),
    )
    logger.info("  Gradient Accumulation steps = %d", args.gradient_accumulation_steps)
    logger.info("  Total optimization steps = %d", t_total)

    model.zero_grad()
    train_iterator = trange(args.num_train_epochs, desc="Epoch", disable=True)

    global_step = 0
    tr_loss = 0.0
    for _ in train_iterator:
        epoch_iterator = tqdm(train_dataloader, desc="Iteration", disable=True)
        for step, batch in enumerate(epoch_iterator):
            source, target, encoder_token_type_ids, encoder_mask, decoder_mask, lm_labels = (
                batch
            )

            source = source.to(args.device)
            target = target.to(args.device)
            encoder_token_type_ids = encoder_token_type_ids.to(args.device)
            encoder_mask = encoder_mask.to(args.device)
            decoder_mask = decoder_mask.to(args.device)
            lm_labels = lm_labels.to(args.device)

            model.train()
            outputs = model(
                source,
                target,
                encoder_token_type_ids=encoder_token_type_ids,
                encoder_attention_mask=encoder_mask,
                decoder_attention_mask=decoder_mask,
                decoder_lm_labels=lm_labels,
            )

            loss = outputs[0]
            logger.info("Current loss: {:.2f}".format(loss.item()))

            if args.gradient_accumulation_steps > 1:
                loss /= args.gradient_accumulation_steps

            loss.backward()

            tr_loss += loss.item()
            if (step + 1) % args.gradient_accumulation_steps == 0:
                torch.nn.utils.clip_grad_norm_(model.parameters(), args.max_grad_norm)
                optimizer.step()
                model.zero_grad()
                global_step += 1

            if args.max_steps > 0 and global_step > args.max_steps:
                epoch_iterator.close()
                break

        if args.max_steps > 0 and global_step > args.max_steps:
            train_iterator.close()
            break

    return global_step, tr_loss / global_step


# ------------------
# Evaluate w/ ROUGE
# ------------------


def evaluate(args, model, tokenizer, path_to_summaries):
    set_seed(args)

    args.eval_batch_size = args.per_gpu_eval_batch_size * max(1, args.n_gpu)
    eval_dataset = load_and_cache_examples(args, tokenizer)
    eval_sampler = SequentialSampler(eval_dataset)
    eval_collate_fn = functools.partial(collate, tokenizer=tokenizer, block_size=512)
    eval_dataloader = DataLoader(
        eval_dataset,
        sampler=eval_sampler,
        batch_size=args.eval_batch_size,
        collate_fn=eval_collate_fn,
    )

    logger.info("***** Running evaluation *****")
    logger.info("  Num examples = %d", len(eval_dataset))
    logger.info("  Batch size = %d", args.eval_batch_size)
    model.eval()

    idx_summary = 0
    for batch in tqdm(eval_dataloader, desc="Evaluating"):
        source, target, encoder_token_type_ids, encoder_mask, _, _ = batch
        source = source.to(args.device)
        target = target.to(args.device)
        encoder_token_type_ids = encoder_token_type_ids.to(args.device)
        encoder_mask = encoder_mask.to(args.device)

        model_kwargs = {
            "encoder_token_type_ids": encoder_token_type_ids,
            "encoder_attention_mask": encoder_mask,
        }

        with torch.no_grad():
            beam = BeamSearch(
                model,
                tokenizer.cls_token_id,
                tokenizer.pad_token_id,
                tokenizer.sep_token_id,
                batch_size=args.eval_batch_size,
                beam_size=5,
                min_length=15,
                max_length=150,
                alpha=0.9,
                block_repeating_trigrams=True,
            )

            results = beam(source, **model_kwargs)

            # keep the best prediction for each sequence
            # blame the ugliness on python for not having an argmax() function
            batch_size = args.eval_batch_size
            best_predictions_idx = [
                max(enumerate(results["scores"][i]), key=lambda x: x[1])[0]
                for i in range(batch_size)
            ]
            summaries_tokens = [
                results["predictions"][idx]
                for b, idx in zip(range(batch_size), best_predictions_idx)
            ]
            for summary_tokens in summaries_tokens:
                summary_tokens = summary_tokens.to("cpu").numpy()
                summary = tokenizer.decode(summary_tokens)
                sentences = summary.split(".")
                sentences = [s + "." for s in sentences]

                path = os.path.join(path_to_summaries, "/model_{}.txt".format(idx_summary))
                with open(path, "w") as output:
                    output.write("\n".join(sentences))
                idx_summary += 1


def save_model_checkpoints(args, model, tokenizer):
    if not os.path.exists(args.output_dir):
        os.makedirs(args.output_dir)

    logger.info("Saving model checkpoint to %s", args.output_dir)

    # Save a trained model, configuration and tokenizer using `save_pretrained()`.
    # They can then be reloaded using `from_pretrained()`
    model_to_save = (
        model.module if hasattr(model, "module") else model
    )  # Take care of distributed/parallel training
    model_to_save.save_pretrained(args.output_dir, model_type="bert")
    tokenizer.save_pretrained(args.output_dir)
    torch.save(args, os.path.join(args.output_dir, "training_arguments.bin"))


def main():
    parser = argparse.ArgumentParser()

    # Required parameters
    parser.add_argument(
        "--data_dir",
        default=None,
        type=str,
        required=True,
        help="The input training data file (a text file).",
    )
    parser.add_argument(
        "--output_dir",
        default=None,
        type=str,
        required=True,
        help="The output directory where the model predictions and checkpoints will be written.",
    )

    # Optional parameters
    parser.add_argument(
        "--gradient_accumulation_steps",
        type=int,
        default=1,
        help="Number of updates steps to accumulate before performing a backward/update pass.",
    )
    parser.add_argument(
        "--do_evaluate",
        type=bool,
        default=False,
        help="Run model evaluation on out-of-sample data.",
    )
    parser.add_argument("--do_train", type=bool, default=False, help="Run training.")
    parser.add_argument(
        "--do_overwrite_output_dir",
        type=bool,
        default=False,
        help="Whether to overwrite the output dir.",
    )
    parser.add_argument(
        "--model_name_or_path",
        default="bert-base-cased",
        type=str,
        help="The model checkpoint to initialize the encoder and decoder's weights with.",
    )
    parser.add_argument(
        "--model_type",
        default="bert",
        type=str,
        help="The decoder architecture to be fine-tuned.",
    )
    parser.add_argument(
        "--max_grad_norm", default=1.0, type=float, help="Max gradient norm."
    )
    parser.add_argument(
        "--max_steps",
        default=-1,
        type=int,
        help="If > 0: set total number of training steps to perform. Override num_train_epochs.",
    )
    parser.add_argument(
        "--to_cpu", default=False, type=bool, help="Whether to force training on CPU."
    )
    parser.add_argument(
        "--num_train_epochs",
        default=10,
        type=int,
        help="Total number of training epochs to perform.",
    )
    parser.add_argument(
        "--per_gpu_train_batch_size",
        default=4,
        type=int,
        help="Batch size per GPU/CPU for training.",
    )
    parser.add_argument(
        "--per_gpu_eval_batch_size",
        default=4,
        type=int,
        help="Batch size per GPU/CPU for evaluation.",
    )
    parser.add_argument("--seed", default=42, type=int)
    args = parser.parse_args()

    if (
        os.path.exists(args.output_dir)
        and os.listdir(args.output_dir)
        and args.do_train
        and not args.do_overwrite_output_dir
    ):
        raise ValueError(
            "Output directory ({}) already exists and is not empty. Use --do_overwrite_output_dir to overwrite.".format(
                args.output_dir
            )
        )

    # Set up training device
    if args.to_cpu or not torch.cuda.is_available():
        args.device = torch.device("cpu")
        args.n_gpu = 0
    else:
        args.device = torch.device("cuda")
        args.n_gpu = torch.cuda.device_count()

    # Load pretrained model. The decoder's weights are randomly initialized.
    # The dropout values for the decoder were taken from Liu & Lapata's repository
    tokenizer = AutoTokenizer.from_pretrained(args.model_name_or_path, do_lower_case=True)
    config = BertConfig.from_pretrained(args.model_name_or_path)
    config.hidden_dropout_prob = 0.2
    config.attention_probs_dropout_prob = 0.2
    decoder_model = BertForMaskedLM(config)
    model = Model2Model.from_pretrained(
        args.model_name_or_path, decoder_model=decoder_model
    )

    # Following Lapata & Liu we share the encoder's word embedding weights with the decoder
    decoder_embeddings = copy.deepcopy(model.encoder.get_input_embeddings())
    model.decoder.set_input_embeddings(decoder_embeddings)

    # Setup logging
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s -   %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO,
    )
    logger.warning(
        "Process rank: %s, device: %s, n_gpu: %s, distributed training: %s, 16-bits training: %s",
        0,
        args.device,
        args.n_gpu,
        False,
        False,
    )

    logger.info("Training/evaluation parameters %s", args)

    # Train the model
    model.to(args.device)
    if args.do_train:
        try:
            global_step, tr_loss = train(args, model, tokenizer)
        except KeyboardInterrupt:
            response = input(
                "You interrupted the training. Do you want to save the model checkpoints? [Y/n]"
            )
            if response.lower() in ["", "y", "yes"]:
                save_model_checkpoints(args, model, tokenizer)
            sys.exit(0)

        logger.info(" global_step = %s, average loss = %s", global_step, tr_loss)
        save_model_checkpoints(args, model, tokenizer)

    # Evaluate the model
    if args.do_evaluate:
        checkpoints = [args.output_dir]
        logger.info("Evaluate the following checkpoints: %s", checkpoints)
        for checkpoint in checkpoints:
            encoder_checkpoint = os.path.join(checkpoint, "bert_encoder")
            decoder_checkpoint = os.path.join(checkpoint, "bert_decoder")
            model = PreTrainedEncoderDecoder.from_pretrained(
                encoder_checkpoint, decoder_checkpoint
            )
            model.to(args.device)

            path_to_generated_summaries = os.path.join(
                args.output_dir, "generated_summaries"
            )
            evaluate(args, model, tokenizer, path_to_generated_summaries)


def create_evaluation_set(args, path_to_formatted_summaries):
    """ Create the evaluation. Pyrouge requires that the lines
    of the summaries should be on separate lines. """
    if not os.path.exists(path_to_formatted_summaries):
        os.makedirs(path_to_formatted_summaries)

    dataset = CNNDailyMailDataset(args.data_dir)
    for i, (_, summary_lines) in enumerate(dataset):
        with open(
            path_to_formatted_summaries + "/original_{}.txt".format(i), "w"
        ) as output:
            output.write("\n".join(summary_lines))


if __name__ == "__main__":
    main()
