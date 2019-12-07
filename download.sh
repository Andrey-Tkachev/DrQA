#!/usr/bin/env bash

# check if requirements are met
REQUIRED=(
    "wget"
    "unzip"
    "python3"
    "pip"
)
for ((i=0;i<${#REQUIRED[@]};++i)); do
    if ! [ -x "$(command -v ${REQUIRED[i]})" ]; then
        echo 'Error: ${REQUIRED[i]} is not installed.' >&2
        exit -1
    fi
done

# Download BOOLQ & GloVe
BOOLQ_DIR=boolq
GLOVE_DIR=glove
mkdir -p $BOOLQ_DIR
mkdir -p $GLOVE_DIR

URLS=(
    "https://storage.cloud.google.com/boolq/train.jsonl"
    "https://storage.cloud.google.com/boolq/dev.jsonl"
    "http://nlp.stanford.edu/data/glove.840B.300d.zip"
)
FILES=(
    "$BOOLQ_DIR/train.jsonl"
    "$BOOLQ_DIR/dev.jsonl"
    "$GLOVE_DIR/glove.840B.300d.zip"
)
for ((i=0;i<${#URLS[@]};++i)); do
    file=${FILES[i]}
    url=${URLS[i]}
    if [ -f $file ]; then
        echo "$file already exists, skipping download."
    else
        wget $url -O $file
        if [ -f $file ]; then
            echo "$url successfully downloaded."
        else
            echo "$url not successfully downloaded."
            exit -1
        fi
        if [ ${file: -4} == ".zip" ]; then
            unzip $file -d "$(dirname "$file")"
        fi
    fi
done

# Download SpaCy English language models
python3 -m spacy download en

