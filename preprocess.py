import tensorflow as tf
import random
from tqdm import tqdm
import spacy
import ujson as json
from collections import Counter
import numpy as np
import os.path
from bilm import Batcher

nlp = spacy.blank("en")


def tokenize_sentence(sent):
    doc = nlp(sent)
    return [token.text for token in doc]


def convert_idx(text, tokens):
    current = 0
    spans = []
    for token in tokens:
        current = text.find(token, current)
        if current < 0:
            print("Token {} cannot be found".format(token))
            raise Exception()
        spans.append((current, current + len(token)))
        current += len(token)
    return spans

# for CoQA dataset
def process_file(filename, data_type, word_counter):
    print("Generating {} examples...".format(data_type))
    examples = []
    total_conversation = 0
    with open(filename, "r") as fh:
        source = json.load(fh)
        for conversation in tqdm(source["data"]):
            context = conversation["story"].replace("''", '" ').replace("``", '" ')
            tokenized_context = tokenize_sentence(context)

            spans = convert_idx(context, tokenized_context)

            for token in tokenized_context:
                word_counter[token] += 1

            questions = conversation["questions"]
            answers = conversation["answers"]

            tokenized_questions = []
            starts = []
            ends = []
            yes_answers, no_answers, unk_answers = [], [], []
            span_flag = []
            total_conversation += 1
            
            for question, answer in zip(questions, answers):
                flag = 1
                ques = question["input_text"].replace("''", '" ').replace("``", '" ')
                tokenized_question = tokenize_sentence(ques)

                for token in tokenized_question:
                    word_counter[token] += 1

                tokenized_questions.append(tokenized_question)

                answer_start = answer["span_start"]
                answer_end = answer["span_end"]

                span_text = answer["span_text"]
                input_text = answer["input_text"]
                tokenized_input_text = tokenize_sentence(input_text)

                yes, no, unk = 0, 0, 0
                if tokenized_input_text[0].lower() == "yes":
                    yes = 1
                if tokenized_input_text[0].lower() == "no":
                    no = 1
                if tokenized_input_text[0].lower() == "unknown":
                    unk = 1

                yes_answers.append(yes)
                no_answers.append(no)
                unk_answers.append(unk)
                answer_span = []

                for idx, span in enumerate(spans):
                    if not (answer_end <= span[0] or answer_start >= span[1]):
                        answer_span.append(idx)

                if not answer_span:
                    flag = 0
                    answer_span.append(-1)
                span_start = answer_span[0]
                span_end = answer_span[-1]
                starts.append(span_start)
                ends.append(span_end)
                span_flag.append(flag)

            example = {"tokenized_context": tokenized_context, "tokenized_questions": tokenized_questions,
                            "starts": starts, "ends": ends, "id": total_conversation, "span_flag": span_flag,
                            "yes_answers": yes_answers, "no_answers": no_answers, "unk_answers": unk_answers}
            examples.append(example)
 
        random.shuffle(examples)
        print("{} conversations in total".format(total_conversation))
    return examples


def get_embedding(counter, data_type, limit=-1, emb_file=None, size=None, vec_size=None, token2idx_dict=None):
    print("Generating {} embedding...".format(data_type))
    embedding_dict = {}
    filtered_elements = [k for k, v in counter.items() if v > limit]
    if emb_file is not None:
        assert size is not None
        assert vec_size is not None
        with open(emb_file, "r", encoding="utf-8") as fh:
            for line in tqdm(fh, total=size):
                array = line.split()
                word = "".join(array[0:-vec_size])
                vector = list(map(float, array[-vec_size:]))
                if word in counter and counter[word] > limit:
                    embedding_dict[word] = vector
        print("{} / {} tokens have corresponding {} embedding vector".format(
            len(embedding_dict), len(filtered_elements), data_type))
    else:
        assert vec_size is not None
        for token in filtered_elements:
            embedding_dict[token] = [np.random.normal(
                scale=0.01) for _ in range(vec_size)]
        print("{} tokens have corresponding embedding vector".format(
            len(filtered_elements)))

    NULL = "--NULL--"
    OOV = "--OOV--"
    token2idx_dict = {token: idx for idx, token in enumerate(
        embedding_dict.keys(), 2)} if token2idx_dict is None else token2idx_dict
    token2idx_dict[NULL] = 0
    token2idx_dict[OOV] = 1

    embedding_dict[NULL] = [0. for _ in range(vec_size)]
    embedding_dict[OOV] = [0. for _ in range(vec_size)]
    idx2emb_dict = {idx: embedding_dict[token]
                    for token, idx in token2idx_dict.items()}
    emb_mat = [idx2emb_dict[idx] for idx in range(len(idx2emb_dict))]

    return emb_mat, token2idx_dict


def build_features(config, examples, data_type, out_file, word2idx_dict, is_test=False):

    para_limit = config.para_limit
    ques_limit = config.ques_limit
    turn_limit = config.turn_limit

    def filter_func(example):
        return len(example["tokenized_context"]) > para_limit #or len(example["ques_tokens"]) > ques_limit

    print("Processing {} examples...".format(data_type))
    writer = tf.python_io.TFRecordWriter(out_file)
    total = 0
    total_ = 0
    meta = {}

    max_char_length = config.max_char_length
    batcher = Batcher(config.elmo_vocab_file, max_char_length)
    for example in tqdm(examples):
        total_ += 1

        if filter_func(example):
            continue

        total += 1
        context_idxs = np.zeros([para_limit], dtype=np.int32)
        questions_idxs = np.zeros([turn_limit, ques_limit], dtype=np.int32)
        context_char_idxs = np.zeros([para_limit + 2, max_char_length], dtype=np.int32)
        questions_char_idxs = np.zeros([turn_limit, ques_limit + 2, max_char_length], dtype=np.int32)
        starts = np.zeros([turn_limit, para_limit], dtype=np.float32)
        ends = np.zeros([turn_limit, para_limit], dtype=np.float32)
        em = np.zeros([turn_limit, para_limit], dtype=np.int32)
        yes_answers = np.zeros([turn_limit], dtype=np.int32)
        no_answers = np.zeros([turn_limit], dtype=np.int32)
        unk_answers = np.zeros([turn_limit], dtype=np.int32)
        span_flag = np.zeros([turn_limit], dtype=np.int32)

        def _get_word(word):
            for each in (word, word.lower(), word.capitalize(), word.upper()):
                if each in word2idx_dict:
                    return word2idx_dict[each]
            return 1

        def _check_word_in_question(word, question):
            for token in question:
                if word.lower() == token.lower():
                    return True
            return False

        # type: List[str]
        tokenized_context = example["tokenized_context"]
        length = len(tokenized_context) + 2
        context_char_idxs_without_mask = batcher._lm_vocab.encode_chars(tokenized_context, split=False)
        context_char_idxs[:length, :] = context_char_idxs_without_mask + 1

        for k, sent in enumerate(example["tokenized_questions"]):
            length = len(sent) + 2
            question_char_idxs_without_mask = batcher._lm_vocab.encode_chars(sent, split=False)
            questions_char_idxs[k, :length, :] = question_char_idxs_without_mask + 1

        # get em and context indexes vector
        for i, token in enumerate(tokenized_context):
            context_idxs[i] = _get_word(token)
            for j, tokenized_question in enumerate(example["tokenized_questions"]):
                if _check_word_in_question(token, tokenized_question):
                    em[j, i] = 1

        # get question indexes vector
        for i, tokenized_question in enumerate(example["tokenized_questions"]):
            for j, token in enumerate(tokenized_question):
                questions_idxs[i, j] = _get_word(token)

        # get start vector
        for i, idx in enumerate(example["starts"]):
            starts[i, idx] = 1.0

        # get end vector
        for i, idx in enumerate(example["ends"]):
            ends[i, idx] = 1.0

        # get label of yes/no questions
        length = len(example["yes_answers"])
        yes_answers[:length] = example["yes_answers"]
        no_answers[:length] = example["no_answers"]
        unk_answers[:length] = example["unk_answers"]
        span_flag[:length] = example["span_flag"]

        feature_dict = {
            "context_idxs": tf.train.Feature(bytes_list=tf.train.BytesList(value=[context_idxs.tostring()])),
            "questions_idxs": tf.train.Feature(bytes_list=tf.train.BytesList(value=[questions_idxs.tostring()])),
            "context_char_idxs":tf.train.Feature(bytes_list=tf.train.BytesList(value=[context_char_idxs.tostring()])),
            "questions_char_idxs":tf.train.Feature(bytes_list=tf.train.BytesList(value=[questions_char_idxs.tostring()])),
            "starts": tf.train.Feature(bytes_list=tf.train.BytesList(value=[starts.tostring()])),
            "ends": tf.train.Feature(bytes_list=tf.train.BytesList(value=[ends.tostring()])),
            "em": tf.train.Feature(bytes_list=tf.train.BytesList(value=[em.tostring()])),
            "yes_answers": tf.train.Feature(bytes_list=tf.train.BytesList(value=[yes_answers.tostring()])),
            "no_answers": tf.train.Feature(bytes_list=tf.train.BytesList(value=[no_answers.tostring()])),
            "unk_answers": tf.train.Feature(bytes_list=tf.train.BytesList(value=[unk_answers.tostring()])),
            "span_flag": tf.train.Feature(bytes_list=tf.train.BytesList(value=[span_flag.tostring()]))
        }

        record = tf.train.Example(features=tf.train.Features(feature=feature_dict))
        writer.write(record.SerializeToString())

    print("Build {} / {} instances of features in total".format(total, total_))
    meta["total"] = total
    writer.close()
    return meta


def save(filename, obj, message=None):
    if message is not None:
        print("Saving {}...".format(message))
        with open(filename, "w") as fh:
            json.dump(obj, fh)


def prepro(config):

    # init counters
    word_counter, char_counter = Counter(), Counter()

    # extract data to examples
    train_examples = process_file(config.train_file, "train", word_counter)
    dev_examples = process_file(config.dev_file, "dev", word_counter)

    # init word-to-index dictionary
    word2idx_dict = None
    if os.path.isfile(config.glove_word2idx_file):
        with open(config.glove_word2idx_file, "r") as fh:
            word2idx_dict = json.load(fh)

    # get embedding matrix
    word_emb_mat, word2idx_dict = get_embedding(word_counter, "word", emb_file=config.glove_word_file,
                                                size=config.glove_word_size, vec_size=config.glove_dim, token2idx_dict=word2idx_dict)

    # save elmo vocab file
    with open(config.elmo_vocab_file, "w") as f:
        print("Saving elmo vocabulary...")
        f.write("</S>\n")
        f.write("<S>\n")
        f.write("<UNK>\n")
        for item in word2idx_dict.keys():
            f.write("%s\n" % item)

    # write train/dev record files
    build_features(config, train_examples, "train", config.train_record_file, word2idx_dict)
    build_features(config, dev_examples, "dev", config.dev_record_file, word2idx_dict)

    # save preprocessed data to files
    save(config.glove_word_emb_file, word_emb_mat, message="word embedding")
    save(config.glove_word2idx_file, word2idx_dict, message="word2idx")

