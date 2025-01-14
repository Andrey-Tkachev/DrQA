import re
import os
import sys
import math
import random
import string
import logging
import argparse
import numpy as np
from shutil import copyfile
from datetime import datetime
from collections import Counter
import torch
import msgpack
from drqa.model import DocReaderModel
from drqa.utils import str2bool
from transformers import BertTokenizer


def main(args=None):
    args, log = setup(args=args)
    log.info('[Program starts. Loading data...]')
    train, dev, dev_y, embedding, opt = load_data(vars(args))
    log.info(opt)
    log.info('[Data loaded.]')
    if args.save_dawn_logs:
        dawn_start = datetime.now()
        log.info('dawn_entry: epoch\taccuracy\thours')

    if args.resume:
        log.info('[loading previous model...]')
        checkpoint = torch.load(os.path.join(args.model_dir, args.resume))
        if args.resume_options:
            opt = checkpoint['config']
        state_dict = checkpoint['state_dict']
        model = DocReaderModel(opt, embedding, state_dict)
        epoch_0 = checkpoint['epoch'] + 1
        # synchronize random seed
        random.setstate(checkpoint['random_state'])
        torch.random.set_rng_state(checkpoint['torch_state'])
        if args.cuda:
            torch.cuda.set_rng_state(checkpoint['torch_cuda_state'])
        if args.reduce_lr:
            lr_decay(model.optimizer, lr_decay=args.reduce_lr)
            log.info('[learning rate reduced by {}]'.format(args.reduce_lr))
        batches = BatchGen(dev, batch_size=args.batch_size, evaluation=True, gpu=args.cuda)
        predictions = []
        for i, batch in enumerate(batches):
            predictions.extend(model.predict(batch))
            log.debug('> evaluating [{}/{}]'.format(i, len(batches)))
        accuracy = score(predictions, dev_y)
        log.info(f"[dev accuracy {accuracy}]")
        if math.fabs(accuracy - checkpoint['accuracy']) > 1e-3:
            log.info('Inconsistent: recorded accuracy'.format(checkpoint['accuracy']))
            log.error('Error loading model: current code is inconsistent with code used to train the previous model.')
            exit(1)
        best_val_score = checkpoint['best_eval']
    else:
        model = DocReaderModel(opt, embedding)
        epoch_0 = 1
        accuracy = 0.0
        best_val_score = 0.0

    for epoch in range(epoch_0, epoch_0 + args.epochs):
        log.warning('Epoch {}'.format(epoch))
        # train
        batches = BatchGen(train, batch_size=args.batch_size, gpu=args.cuda, bert=opt['bert'])
        start = datetime.now()
        for i, batch in enumerate(batches):
            model.update(batch)
            if i % args.log_per_updates == 0:
                log.info('> epoch [{0:2}] updates[{1:6}] train loss[{2:.5f}] remaining[{3}]'.format(
                    epoch, model.updates, model.train_loss.value,
                    str((datetime.now() - start) / (i + 1) * (len(batches) - i - 1)).split('.')[0]))
        log.debug('\n')
        # eval
        batches = BatchGen(dev, batch_size=args.batch_size, evaluation=True, gpu=args.cuda, bert=opt['bert'])
        predictions = []
        for i, batch in enumerate(batches):
            predictions.extend(model.predict(batch))
            log.debug('> evaluating [{}/{}]'.format(i, len(batches)))
        accuracy = score(predictions, dev_y)
        log.warning("dev accuracy {}".format(accuracy))
        if args.save_dawn_logs:
            time_diff = datetime.now() - dawn_start
            log.warning("dawn_entry: {}\t{}\t{}".format(epoch, accuracy, float(time_diff.total_seconds() / 3600.0)))
        # save
        if not args.save_last_only or epoch == epoch_0 + args.epochs - 1:
            model_file = os.path.join(args.model_dir, 'checkpoint_epoch_{}.pt'.format(epoch))
            model.save(model_file, epoch, [accuracy, best_val_score])
            if accuracy > best_val_score:
                best_val_score = accuracy
                copyfile(
                    model_file,
                    os.path.join(args.model_dir, 'best_model.pt'))
                log.info('[new best model saved.]')
    return best_val_score


def setup(args=None):
    parser = argparse.ArgumentParser(
        description='Train a Document Reader model.'
    )
    # system
    parser.add_argument('--log_per_updates', type=int, default=3,
                        help='log model loss per x updates (mini-batches).')
    parser.add_argument('--data_file', default='boolq/data.msgpack',
                        help='path to preprocessed data file.')
    parser.add_argument('--model_dir', default='models',
                        help='path to store saved models.')
    parser.add_argument('--save_last_only', action='store_true',
                        help='only save the final models.')
    parser.add_argument('--save_dawn_logs', action='store_true',
                        help='append dawnbench log entries prefixed with dawn_entry:')
    parser.add_argument('--seed', type=int, default=1013,
                        help='random seed for data shuffling, dropout, etc.')
    parser.add_argument("--cuda", type=str2bool, nargs='?',
                        const=True, default=torch.cuda.is_available(),
                        help='whether to use GPU acceleration.')
    # training
    parser.add_argument('-e', '--epochs', type=int, default=40)
    parser.add_argument('-bs', '--batch_size', type=int, default=32)
    parser.add_argument('-rs', '--resume', default='best_model.pt',
                        help='previous model file name (in `model_dir`). '
                             'e.g. "checkpoint_epoch_11.pt"')
    parser.add_argument('-ro', '--resume_options', action='store_true',
                        help='use previous model options, ignore the cli and defaults.')
    parser.add_argument('-rlr', '--reduce_lr', type=float, default=0.,
                        help='reduce initial (resumed) learning rate by this factor.')
    parser.add_argument('-op', '--optimizer', default='adamax',
                        help='supported optimizer: adamax, sgd')
    parser.add_argument('-gc', '--grad_clipping', type=float, default=10)
    parser.add_argument('-wd', '--weight_decay', type=float, default=0)
    parser.add_argument('-lr', '--learning_rate', type=float, default=0.1,
                        help='only applied to SGD.')
    parser.add_argument('-mm', '--momentum', type=float, default=0,
                        help='only applied to SGD.')
    parser.add_argument('-tp', '--tune_partial', type=int, default=1000,
                        help='finetune top-x embeddings.')
    parser.add_argument('--fix_embeddings', action='store_true',
                        help='if true, `tune_partial` will be ignored.')
    parser.add_argument('--rnn_padding', action='store_true',
                        help='perform rnn padding (much slower but more accurate).')
    # model
    parser.add_argument('--question_merge', default='self_attn')
    parser.add_argument('--doc_layers', type=int, default=3)
    parser.add_argument('--question_layers', type=int, default=3)
    parser.add_argument('--hidden_size', type=int, default=128)
    parser.add_argument('--num_features', type=int, default=4)
    parser.add_argument('--pos', type=str2bool, nargs='?', const=True, default=True,
                        help='use pos tags as a feature.')
    parser.add_argument('--ner', type=str2bool, nargs='?', const=True, default=True,
                        help='use named entity tags as a feature.')
    parser.add_argument('--use_qemb', type=str2bool, nargs='?', const=True, default=True)
    parser.add_argument('--concat_rnn_layers', type=str2bool, nargs='?',
                        const=True, default=True)
    parser.add_argument('--dropout_emb', type=float, default=0.4)
    parser.add_argument('--dropout_rnn', type=float, default=0.4)
    parser.add_argument('--dropout_rnn_output', type=str2bool, nargs='?',
                        const=True, default=True)
    parser.add_argument('--max_len', type=int, default=15)
    parser.add_argument('--rnn_type', default='lstm',
                        help='supported types: rnn, gru, lstm')

    args = parser.parse_args(args=args)

    # set model dir
    model_dir = args.model_dir
    os.makedirs(model_dir, exist_ok=True)
    args.model_dir = os.path.abspath(model_dir)

    if args.resume == 'best_model.pt' and not os.path.exists(os.path.join(args.model_dir, args.resume)):
        # means we're starting fresh
        args.resume = ''

    # set random seed
    random.seed(args.seed)
    torch.manual_seed(args.seed)
    if args.cuda:
        torch.cuda.manual_seed(args.seed)

    # setup logger
    class ProgressHandler(logging.Handler):
        def __init__(self, level=logging.NOTSET):
            super().__init__(level)

        def emit(self, record):
            log_entry = self.format(record)
            if record.message.startswith('> '):
                sys.stdout.write('{}\r'.format(log_entry.rstrip()))
                sys.stdout.flush()
            else:
                sys.stdout.write('{}\n'.format(log_entry))

    log = logging.getLogger(__name__)
    log.setLevel(logging.DEBUG)
    fh = logging.FileHandler(os.path.join(args.model_dir, 'log.txt'))
    fh.setLevel(logging.INFO)
    ch = ProgressHandler()
    ch.setLevel(logging.DEBUG)
    formatter = logging.Formatter(fmt='%(asctime)s %(message)s', datefmt='%m/%d/%Y %I:%M:%S')
    fh.setFormatter(formatter)
    ch.setFormatter(formatter)
    log.addHandler(fh)
    log.addHandler(ch)

    return args, log


def lr_decay(optimizer, lr_decay):
    for param_group in optimizer.param_groups:
        param_group['lr'] *= lr_decay
    return optimizer


def load_data(opt):
    with open('meta/meta.msgpack', 'rb') as f:
        meta = msgpack.load(f, encoding='utf8')
    opt['bert'] = meta['bert']
    if not opt['bert']:
        embedding = torch.Tensor(meta['embedding'])
        opt['pretrained_words'] = True
        opt['vocab_size'] = embedding.size(0)
        opt['embedding_dim'] = embedding.size(1)
        opt['pos_size'] = len(meta['vocab_tag'])
        opt['ner_size'] = len(meta['vocab_ent'])
        BatchGen.pos_size = opt['pos_size']
        BatchGen.ner_size = opt['ner_size']
    else:
        embedding = None
        opt['pretrained_words'] = False
        opt['embedding_dim'] = 768
    with open(opt['data_file'], 'rb') as f:
        data = msgpack.load(f, encoding='utf8')
    train = data['train']
    data['dev'].sort(key=lambda x: len(x[1]))
    dev = [x[:-1] for x in data['dev']]
    dev_y = [x[-1] for x in data['dev']]
    return train, dev, dev_y, embedding, opt


class BatchGen:
    pos_size = None
    ner_size = None

    def __init__(self, data, batch_size, gpu, evaluation=False, bert=False):
        """
        input:
            data - list of lists
            batch_size - int
        """
        self.batch_size = batch_size
        self.eval = evaluation
        self.gpu = gpu
        self.bert = bert
        self.pad_token = 0
        if bert:
            self.pad_token = BertTokenizer.from_pretrained('bert-base-uncased').pad_token_id

        # sort by len
        data = sorted(data, key=lambda x: len(x[1]))
        # chunk into batches
        data = [data[i:i + batch_size] for i in range(0, len(data), batch_size)]

        # shuffle
        if not evaluation:
            random.shuffle(data)

        self.data = data

    def __len__(self):
        return len(self.data)

    def __iter__(self):
        for batch in self.data:
            batch_size = len(batch)
            batch = list(zip(*batch))
            if self.eval:
                assert len(batch) == 8, f'Eval expected f{len(batch)}'
            else:
                assert len(batch) == 9, f'Train expected f{len(batch)}'

            context_len = max(len(x) for x in batch[1])
            context_id = torch.LongTensor(batch_size, context_len).fill_(self.pad_token)
            for i, doc in enumerate(batch[1]):
                context_id[i, :len(doc)] = torch.LongTensor(doc)

            if not self.bert:
                feature_len = len(batch[2][0][0])

                context_feature = torch.Tensor(batch_size, context_len, feature_len).fill_(0)
                for i, doc in enumerate(batch[2]):
                    for j, feature in enumerate(doc):
                        context_feature[i, j, :] = torch.Tensor(feature)

                context_tag = torch.Tensor(batch_size, context_len, self.pos_size).fill_(0)
                for i, doc in enumerate(batch[3]):
                    for j, tag in enumerate(doc):
                        context_tag[i, j, tag] = 1

                context_ent = torch.Tensor(batch_size, context_len, self.ner_size).fill_(0)
                for i, doc in enumerate(batch[4]):
                    for j, ent in enumerate(doc):
                        context_ent[i, j, ent] = 1
            else:
                context_feature = None
                context_tag = None
                context_ent = None

            question_len = max(len(x) for x in batch[5])
            question_id = torch.LongTensor(batch_size, question_len).fill_(self.pad_token)
            for i, doc in enumerate(batch[5]):
                question_id[i, :len(doc)] = torch.LongTensor(doc)

            context_mask = torch.eq(context_id, self.pad_token)
            question_mask = torch.eq(question_id, self.pad_token)
            text = list(batch[6])
            span = list(batch[7])
            if not self.eval:
                answ = torch.FloatTensor(batch[8])
            if self.gpu:
                context_id = context_id.pin_memory()
                if not self.bert:
                    context_feature = context_feature.pin_memory()
                    context_tag = context_tag.pin_memory()
                    context_ent = context_ent.pin_memory()
                else:
                    context_feature = None
                    context_tag = None
                    context_ent = None
                context_mask = context_mask.pin_memory()
                question_id = question_id.pin_memory()
                question_mask = question_mask.pin_memory()
            if self.eval:
                yield (context_id, context_feature, context_tag, context_ent, context_mask,
                       question_id, question_mask, text, span, None)
            else:
                yield (context_id, context_feature, context_tag, context_ent, context_mask,
                       question_id, question_mask, text, span, answ)

def score(pred, truth):
    assert len(pred) == len(truth)
    return ((np.array(pred) == np.array(truth)).sum() + 0.0) / len(truth)


if __name__ == '__main__':
    main()

