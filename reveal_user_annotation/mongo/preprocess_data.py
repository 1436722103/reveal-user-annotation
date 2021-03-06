__author__ = 'Georgios Rizos (georgerizos@iti.gr)'

import multiprocessing as mp
import itertools
import time

import numpy as np
import scipy.sparse as spsp
import networkx as nx
import networkx.algorithms.components as nxalgcom
from pymongo import ASCENDING

from reveal_user_annotation.text.clean_text import clean_document, combine_word_list, get_lemmatizer, get_stopset,\
    get_camel_case_regexes, get_digits_punctuation_whitespace_regex, get_pos_set, backoff_tagger, get_word_patterns,\
    get_braupt_tagger, get_tokenizer


# def get_user_to_bag_of_words_dictionary(user_twitter_id_list, database):
#     """
#     Returns a python dictionary that maps Twitter user ids to a bag-of-words.
#
#     Inputs: - user_twitter_id_list: A python list of Twitter user ids.
#             - database: A Mongo database object.
#
#     Output: - user_to_bag_of_words_dictionary: A python dictionary that maps Twitter user ids to a bag-of-words.
#     """
#     user_to_bag_of_words_dictionary = dict()
#     for user_twitter_id, bag_of_words in read_bag_of_words_for_each_user(user_twitter_id_list, database):
#         user_to_bag_of_words_dictionary[user_twitter_id] = bag_of_words
#
#     return user_to_bag_of_words_dictionary
#
#
# def read_bag_of_words_for_each_user(user_twitter_id_list, database):
#     """
#     For each Twitter user ids, it reads the corresponding preprocessed bag-of-words.
#
#     Inputs:  - user_twitter_id_list: A python list of Twitter user ids.
#              - database: A Mongo database object.
#
#     Yields: - user_twitter_id: A Twitter user id.
#             - bag_of_words: A python dictionary that maps keywords to multiplicity.
#     """
#     for user_twitter_id in user_twitter_id_list:
#         collection_name = str(user_twitter_id)
#         user_keywords_collection = database[collection_name]
#
#         user_keywords_cursor = user_keywords_collection.find()
#         bag_of_words = next(user_keywords_cursor)
#
#         yield user_twitter_id, bag_of_words


def store_user_documents(user_document_gen, client, mongo_database_name, mongo_collection_name):
    """
    Stores Twitter list objects that a Twitter user is a member of in different mongo collections.

    Inputs: - user_document_gen: A python generator that yields a Twitter user id and an associated document list.
            - client: A pymongo MongoClient object.
            - mongo_database_name: The name of a Mongo database as a string.
            - mongo_collection_name: The name of the mongo collection as a string.
    """
    mongo_database = client[mongo_database_name]
    mongo_collection = mongo_database[mongo_collection_name]

    # Iterate over all users to be annotated and store the Twitter lists in mongo.
    for user_twitter_id, user_document_list in user_document_gen:
        document = user_document_list
        document["_id"] = int(user_twitter_id)
        mongo_collection.update({"_id": user_twitter_id}, document, upsert=True)


def read_user_documents_for_single_user_generator(user_twitter_id, mongo_database):
    """
    Stores Twitter list objects that a Twitter user is a member of in different mongo collections.

    Inputs: - user_twitter_id: A Twitter user id.
            - mongo_database: A mongo database.

    Yields: - twitter_list: A tuple containing: * A Twitter user id.
                                                * A python list containing Twitter lists in dictionary (json) format.
    """
    collection_name = str(user_twitter_id)
    collection = mongo_database[collection_name]
    cursor = collection.find()

    for twitter_list in cursor:
        yield twitter_list


def read_user_documents_generator(user_twitter_id_list, client, mongo_database_name, mongo_collection_name):
    """
    Stores Twitter list objects that a Twitter user is a member of in different mongo collections.

    Inputs: - user_twitter_id_list: A python list of Twitter user ids.
            - client: A pymongo MongoClient object.
            - mongo_database_name: The name of a Mongo database as a string.
            - mongo_collection_name: The name of the mongo collection as a string.

    Yields: - user_twitter_id: A Twitter user id.
            - twitter_list_gen: A python generator that yields Twitter lists in dictionary (json) format.
    """
    mongo_database = client[mongo_database_name]
    mongo_collection = mongo_database[mongo_collection_name]

    # cursor = mongo_collection.find({"_id": {"$in": [int(user_twitter_id) for user_twitter_id in user_twitter_id_list]}})
    cursor = mongo_collection.find()

    user_twitter_id_list = [int(user_twitter_id) for user_twitter_id in user_twitter_id_list]
    user_twitter_id_list = set(user_twitter_id_list)

    for documents in cursor:
        if documents["_id"] in user_twitter_id_list:
            yield documents["_id"], documents


def get_collection_documents_generator(client, database_name, collection_name, spec, latest_n, sort_key):
    """
    This is a python generator that yields tweets stored in a mongodb collection.

    Tweet "created_at" field is assumed to have been stored in the format supported by MongoDB.

    Inputs: - client: A pymongo MongoClient object.
            - database_name: The name of a Mongo database as a string.
            - collection_name: The name of the tweet collection as a string.
            - spec: A python dictionary that defines higher query arguments.
            - latest_n: The number of latest results we require from the mongo document collection.
            - sort_key: A field name according to which we will sort in ascending order.

    Yields: - document: A document in python dictionary (json) format.
    """
    mongo_database = client[database_name]
    collection = mongo_database[collection_name]
    collection.create_index(sort_key)

    if latest_n is not None:
        skip_n = collection.count() - latest_n
        if collection.count() - latest_n < 0:
            skip_n = 0
        cursor = collection.find(filter=spec).sort([(sort_key, ASCENDING), ])
        cursor = cursor[skip_n:]
    else:
        cursor = collection.find(filter=spec).sort([(sort_key, ASCENDING), ])

    for document in cursor:
        yield document


def extract_graphs_and_lemmas_from_tweets(tweet_generator):
    """
    Given a tweet python generator, we encode the information into mention and retweet graphs and a lemma matrix.

    We assume that the tweets are given in increasing timestamp.

    Inputs:  - tweet_generator: A python generator of tweets in python dictionary (json) format.

    Outputs: - mention_graph: The mention graph as a SciPy sparse matrix.
             - retweet_graph: The retweet graph as a SciPy sparse matrix.
             - user_lemma_matrix: The user lemma vector representation matrix as a SciPy sparse matrix.
             - tweet_id_set: A python set containing the Twitter ids for all the dataset tweets.
             - user_id_set: A python set containing the Twitter ids for all the dataset users.
             - lemma_to_attribute: A map from lemmas to numbers in python dictionary format.
    """
    ####################################################################################################################
    # Prepare for iterating over tweets.
    ####################################################################################################################
    # These are initialized as lists for incremental extension.
    tweet_id_set = set()
    user_id_set = list()

    add_tweet_id = tweet_id_set.add
    append_user_id = user_id_set.append

    # Initialize sparse matrix arrays.
    mention_graph_row = list()
    mention_graph_col = list()

    retweet_graph_row = list()
    retweet_graph_col = list()

    user_lemma_matrix_row = list()
    user_lemma_matrix_col = list()
    user_lemma_matrix_data = list()

    append_mention_graph_row = mention_graph_row.append
    append_mention_graph_col = mention_graph_col.append

    append_retweet_graph_row = retweet_graph_row.append
    append_retweet_graph_col = retweet_graph_col.append

    extend_user_lemma_matrix_row = user_lemma_matrix_row.extend
    extend_user_lemma_matrix_col = user_lemma_matrix_col.extend
    extend_user_lemma_matrix_data = user_lemma_matrix_data.extend

    # Initialize dictionaries.
    id_to_node = dict()
    id_to_name = dict()
    id_to_username = dict()
    id_to_listedcount = dict()
    lemma_to_attribute = dict()

    sent_tokenize, _treebank_word_tokenize = get_tokenizer()
    # tagger = HunposTagger('hunpos-1.0-linux/english.model', 'hunpos-1.0-linux/hunpos-tag')
    # tagger = PerceptronTagger()
    tagger = get_braupt_tagger()
    lemmatizer, lemmatize = get_lemmatizer("wordnet")
    stopset = get_stopset()
    first_cap_re, all_cap_re = get_camel_case_regexes()
    digits_punctuation_whitespace_re = get_digits_punctuation_whitespace_regex()
    pos_set = get_pos_set()

    ####################################################################################################################
    # Iterate over tweets.
    ####################################################################################################################
    counter = 0
    for tweet in tweet_generator:
        # print(tweet)
        # Increment tweet counter.
        counter += 1
        # if counter % 10000 == 0:
        #     print(counter)
        # print(counter)

        # Extract base tweet's values.
        try:
            tweet_id = tweet["id"]
            user_id = tweet["user"]["id"]
            user_name = tweet["user"]["name"]
            user_screen_name = tweet["user"]["screen_name"]

            listed_count_raw = tweet["user"]["listed_count"]

            tweet_text = tweet["text"]

            tweet_in_reply_to_user_id = tweet["in_reply_to_user_id"]
            tweet_in_reply_to_screen_name = tweet["in_reply_to_screen_name"]
            tweet_entities_user_mentions = tweet["entities"]["user_mentions"]

            if "retweeted_status" not in tweet.keys():
                user_mention_id_list = list()
                user_mention_screen_name_list = list()
                for user_mention in tweet_entities_user_mentions:
                    user_mention_id_list.append(user_mention["id"])
                    user_mention_screen_name_list.append(user_mention["screen_name"])
            else:
                # Extract base tweet's values.
                original_tweet = tweet["retweeted_status"]

                original_tweet_id = original_tweet["id"]
                original_tweet_user_id = original_tweet["user"]["id"]
                original_tweet_user_name = original_tweet["user"]["name"]
                original_tweet_user_screen_name = original_tweet["user"]["screen_name"]

                listed_count_raw = original_tweet["user"]["listed_count"]

                original_tweet_text = original_tweet["text"]

                original_tweet_in_reply_to_user_id = original_tweet["in_reply_to_user_id"]
                original_tweet_in_reply_to_screen_name = original_tweet["in_reply_to_screen_name"]
                original_tweet_entities_user_mentions = original_tweet["entities"]["user_mentions"]

                original_tweet_user_mention_id_list = list()
                original_tweet_user_mention_screen_name_list = list()
                for user_mention in original_tweet_entities_user_mentions:
                    original_tweet_user_mention_id_list.append(user_mention["id"])
                    original_tweet_user_mention_screen_name_list.append(user_mention["screen_name"])
        except KeyError:
            continue
        # Map users to distinct integer numbers.
        graph_size = len(id_to_node)
        source_node = id_to_node.setdefault(user_id, graph_size)

        if listed_count_raw is None:
            id_to_listedcount[user_id] = 0
        else:
            id_to_listedcount[user_id] = int(listed_count_raw)

        # Update sets, lists and dictionaries.
        add_tweet_id(tweet_id)
        id_to_name[user_id] = user_screen_name
        id_to_username[user_id] = user_name
        append_user_id(user_id)

        ################################################################################################################
        # We are dealing with an original tweet.
        ################################################################################################################
        if "retweeted_status" not in tweet.keys():
            ############################################################################################################
            # Update user-lemma frequency matrix.
            ############################################################################################################
            # Extract lemmas from the text.
            tweet_lemmas, lemma_to_keywordbag = clean_document(tweet_text, sent_tokenize, _treebank_word_tokenize,
                                                               tagger, lemmatizer, lemmatize, stopset,
                                                               first_cap_re, all_cap_re, digits_punctuation_whitespace_re,
                                                               pos_set)

            number_of_lemmas = len(tweet_lemmas)

            # Update the user-lemma frequency matrix one-by-one.
            attribute_list = list()
            append_attribute = attribute_list.append
            for lemma in tweet_lemmas:
                # Map lemmas to distinct integer numbers.
                vocabulary_size = len(lemma_to_attribute)
                attribute = lemma_to_attribute.setdefault(lemma, vocabulary_size)
                append_attribute(attribute)

            # Add values to the sparse matrix arrays.
            extend_user_lemma_matrix_row(number_of_lemmas*[source_node])
            extend_user_lemma_matrix_col(attribute_list)
            extend_user_lemma_matrix_data(number_of_lemmas*[1.0])

            ############################################################################################################
            # Update mention matrix.
            ############################################################################################################
            # Get mentioned user ids.
            mentioned_user_id_set = list()
            if tweet_in_reply_to_user_id is not None:
                mentioned_user_id_set.append(tweet_in_reply_to_user_id)

                id_to_name[tweet_in_reply_to_user_id] = tweet_in_reply_to_screen_name
            for user_mention, mentioned_user_id, mentioned_user_screen_name in zip(tweet_entities_user_mentions,
                                                                                   user_mention_id_list,
                                                                                   user_mention_screen_name_list):
                mentioned_user_id_set.append(mentioned_user_id)

                id_to_name[mentioned_user_id] = mentioned_user_screen_name

            # We remove duplicates.
            mentioned_user_id_set = set(mentioned_user_id_set)

            # Update the mention graph one-by-one.
            for mentioned_user_id in mentioned_user_id_set:
                # Map users to distinct integer numbers.
                graph_size = len(id_to_node)
                mention_target_node = id_to_node.setdefault(mentioned_user_id, graph_size)

                append_user_id(mentioned_user_id)

                # Add values to the sparse matrix arrays.
                append_mention_graph_row(source_node)
                append_mention_graph_col(mention_target_node)

        ################################################################################################################
        # We are dealing with a retweet.
        ################################################################################################################
        else:
            # Map users to distinct integer numbers.
            graph_size = len(id_to_node)
            original_tweet_node = id_to_node.setdefault(original_tweet_user_id, graph_size)

            if listed_count_raw is None:
                id_to_listedcount[user_id] = 0
            else:
                id_to_listedcount[user_id] = int(listed_count_raw)

            # Update retweet graph.
            append_retweet_graph_row(source_node)
            append_retweet_graph_col(original_tweet_node)

            # Extract lemmas from the text.
            tweet_lemmas, lemma_to_keywordbag = clean_document(original_tweet_text, sent_tokenize, _treebank_word_tokenize,
                                                               tagger, lemmatizer, lemmatize, stopset,
                                                               first_cap_re, all_cap_re, digits_punctuation_whitespace_re,
                                                               pos_set)

            number_of_lemmas = len(tweet_lemmas)

            # Update the user-lemma frequency matrix one-by-one.
            attribute_list = list()
            append_attribute = attribute_list.append
            for lemma in tweet_lemmas:
                # Map lemmas to distinct integer numbers.
                vocabulary_size = len(lemma_to_attribute)
                attribute = lemma_to_attribute.setdefault(lemma, vocabulary_size)
                append_attribute(attribute)

            # Add values to the sparse matrix arrays.
            extend_user_lemma_matrix_row(number_of_lemmas*[source_node])
            extend_user_lemma_matrix_col(attribute_list)
            extend_user_lemma_matrix_data(number_of_lemmas*[1.0])

            # Get mentioned user ids.
            mentioned_user_id_set = list()
            if original_tweet_in_reply_to_user_id is not None:
                mentioned_user_id_set.append(original_tweet_in_reply_to_user_id)

                id_to_name[original_tweet_in_reply_to_user_id] = original_tweet_in_reply_to_screen_name
            for user_mention, mentioned_user_id, mentioned_user_screen_name in zip(original_tweet_entities_user_mentions,
                                                                                   original_tweet_user_mention_id_list,
                                                                                   original_tweet_user_mention_screen_name_list):
                mentioned_user_id_set.append(mentioned_user_id)

                id_to_name[mentioned_user_id] = mentioned_user_screen_name

            # We remove duplicates.
            mentioned_user_id_set = set(mentioned_user_id_set)

            # Get mentioned user ids.
            retweet_mentioned_user_id_set = list()
            if original_tweet_in_reply_to_user_id is not None:
                retweet_mentioned_user_id_set.append(original_tweet_in_reply_to_user_id)

                id_to_name[original_tweet_in_reply_to_user_id] = original_tweet_in_reply_to_screen_name
            for user_mention, mentioned_user_id, mentioned_user_screen_name in zip(original_tweet_entities_user_mentions,
                                                                                   original_tweet_user_mention_id_list,
                                                                                   original_tweet_user_mention_screen_name_list):
                retweet_mentioned_user_id_set.append(mentioned_user_id)

                id_to_name[mentioned_user_id] = mentioned_user_screen_name

            # We remove duplicates.
            retweet_mentioned_user_id_set = set(retweet_mentioned_user_id_set)

            mentioned_user_id_set.update(retweet_mentioned_user_id_set)

            # Update the mention graph one-by-one.
            for mentioned_user_id in mentioned_user_id_set:
                # Map users to distinct integer numbers.
                graph_size = len(id_to_node)
                mention_target_node = id_to_node.setdefault(mentioned_user_id, graph_size)

                append_user_id(mentioned_user_id)

                # Add values to the sparse matrix arrays.
                append_mention_graph_row(source_node)
                append_mention_graph_col(mention_target_node)

            # This is the first time we deal with this tweet.
            if original_tweet_id not in tweet_id_set:
                # Update sets, lists and dictionaries.
                add_tweet_id(original_tweet_id)
                id_to_name[original_tweet_user_id] = original_tweet_user_screen_name
                id_to_username[original_tweet_user_id] = original_tweet_user_name
                append_user_id(original_tweet_user_id)

                ########################################################################################################
                # Update user-lemma frequency matrix.
                ########################################################################################################
                # Update the user-lemma frequency matrix one-by-one.
                attribute_list = list()
                append_attribute = attribute_list.append
                for lemma in tweet_lemmas:
                    # Map lemmas to distinct integer numbers.
                    vocabulary_size = len(lemma_to_attribute)
                    attribute = lemma_to_attribute.setdefault(lemma, vocabulary_size)
                    append_attribute(attribute)

                # Add values to the sparse matrix arrays.
                extend_user_lemma_matrix_row(number_of_lemmas*[source_node])
                extend_user_lemma_matrix_col(attribute_list)
                extend_user_lemma_matrix_data(number_of_lemmas*[1.0])

                ########################################################################################################
                # Update mention matrix.
                ########################################################################################################
                # Update the mention graph one-by-one.
                for mentioned_user_id in retweet_mentioned_user_id_set:
                    # Map users to distinct integer numbers.
                    graph_size = len(id_to_node)
                    mention_target_node = id_to_node.setdefault(mentioned_user_id, graph_size)

                    append_user_id(mentioned_user_id)

                    # Add values to the sparse matrix arrays.
                    append_mention_graph_row(original_tweet_node)
                    append_mention_graph_col(mention_target_node)
            else:
                pass

    ####################################################################################################################
    # Final steps of preprocessing tweets.
    ####################################################################################################################
    # Discard any duplicates.
    user_id_set = set(user_id_set)
    number_of_users = len(user_id_set)
    # min_number_of_users = max(user_id_set) + 1

    # Form mention graph adjacency matrix.
    mention_graph_row = np.array(mention_graph_row, dtype=np.int64)
    mention_graph_col = np.array(mention_graph_col, dtype=np.int64)
    mention_graph_data = np.ones_like(mention_graph_row, dtype=np.float64)

    mention_graph = spsp.coo_matrix((mention_graph_data, (mention_graph_row, mention_graph_col)),
                                    shape=(number_of_users, number_of_users))
    mention_graph = spsp.coo_matrix(spsp.csr_matrix(mention_graph))

    # Form retweet graph adjacency matrix.
    retweet_graph_row = np.array(retweet_graph_row, dtype=np.int64)
    retweet_graph_col = np.array(retweet_graph_col, dtype=np.int64)
    retweet_graph_data = np.ones_like(retweet_graph_row, dtype=np.float64)

    retweet_graph = spsp.coo_matrix((retweet_graph_data, (retweet_graph_row, retweet_graph_col)),
                                    shape=(number_of_users, number_of_users))
    retweet_graph = spsp.coo_matrix(spsp.csr_matrix(retweet_graph))

    # Form user-lemma matrix.
    number_of_lemmas = len(lemma_to_attribute)

    user_lemma_matrix_row = np.array(user_lemma_matrix_row, dtype=np.int64)
    user_lemma_matrix_col = np.array(user_lemma_matrix_col, dtype=np.int64)
    user_lemma_matrix_data = np.array(user_lemma_matrix_data, dtype=np.float64)

    user_lemma_matrix = spsp.coo_matrix((user_lemma_matrix_data, (user_lemma_matrix_row, user_lemma_matrix_col)),
                                        shape=(number_of_users, number_of_lemmas))
    user_lemma_matrix = spsp.coo_matrix(spsp.csr_matrix(user_lemma_matrix))

    node_to_id = dict(zip(id_to_node.values(), id_to_node.keys()))

    # tagger.close()

    return mention_graph, retweet_graph, user_lemma_matrix, tweet_id_set, user_id_set, node_to_id, lemma_to_attribute, id_to_name, id_to_username, id_to_listedcount


def extract_graphs_from_tweets(tweet_generator):
    """
    Given a tweet python generator, we encode the information into mention and retweet graphs.

    We assume that the tweets are given in increasing timestamp.

    Inputs:  - tweet_generator: A python generator of tweets in python dictionary (json) format.

    Outputs: - mention_graph: The mention graph as a SciPy sparse matrix.
             - user_id_set: A python set containing the Twitter ids for all the dataset users.
             - node_to_id: A python dictionary that maps from node anonymized ids, to twitter user ids.
    """
    ####################################################################################################################
    # Prepare for iterating over tweets.
    ####################################################################################################################
    # These are initialized as lists for incremental extension.
    tweet_id_set = set()
    user_id_set = list()
    twitter_to_reveal_user_id = dict()

    add_tweet_id = tweet_id_set.add
    append_user_id = user_id_set.append

    # Initialize sparse matrix arrays.
    mention_graph_row = list()
    mention_graph_col = list()

    retweet_graph_row = list()
    retweet_graph_col = list()

    append_mention_graph_row = mention_graph_row.append
    append_mention_graph_col = mention_graph_col.append

    append_retweet_graph_row = retweet_graph_row.append
    append_retweet_graph_col = retweet_graph_col.append

    # Initialize dictionaries.
    id_to_node = dict()
    id_to_name = dict()
    id_to_username = dict()
    id_to_listedcount = dict()

    ####################################################################################################################
    # Iterate over tweets.
    ####################################################################################################################
    counter = 0
    for tweet in tweet_generator:
        # Increment tweet counter.
        counter += 1
        if counter % 10000 == 0:
            print(counter)
        # print(counter)

        # Extract base tweet's values.
        try:
            tweet_id = tweet["id"]
            user_id = tweet["user"]["id"]
            user_screen_name = tweet["user"]["screen_name"]
            user_name = tweet["user"]["name"]

            listed_count_raw = tweet["user"]["listed_count"]

            tweet_in_reply_to_user_id = tweet["in_reply_to_user_id"]
            tweet_in_reply_to_screen_name = tweet["in_reply_to_screen_name"]
            tweet_entities_user_mentions = tweet["entities"]["user_mentions"]
        except KeyError:
            continue

        # Map users to distinct integer numbers.
        graph_size = len(id_to_node)
        source_node = id_to_node.setdefault(user_id, graph_size)

        if listed_count_raw is None:
            id_to_listedcount[user_id] = 0
        else:
            id_to_listedcount[user_id] = int(listed_count_raw)

        # Update sets, lists and dictionaries.
        add_tweet_id(tweet_id)
        id_to_name[user_id] = user_screen_name
        id_to_username[user_id] = user_name
        append_user_id(user_id)

        # twitter_to_user_id

        ################################################################################################################
        # We are dealing with an original tweet.
        ################################################################################################################
        if "retweeted_status" not in tweet.keys():
            ############################################################################################################
            # Update mention matrix.
            ############################################################################################################
            # Get mentioned user ids.
            mentioned_user_id_set = list()
            if tweet_in_reply_to_user_id is not None:
                mentioned_user_id_set.append(tweet_in_reply_to_user_id)

                id_to_name[tweet_in_reply_to_user_id] = tweet_in_reply_to_screen_name
            for user_mention in tweet_entities_user_mentions:
                mentioned_user_id = user_mention["id"]  # TODO: Perhaps safe extract as well.
                mentioned_user_id_set.append(mentioned_user_id)

                id_to_name[mentioned_user_id] = user_mention["screen_name"]  # TODO: Perhaps safe extract as well.

            # We remove duplicates.
            mentioned_user_id_set = set(mentioned_user_id_set)

            # Update the mention graph one-by-one.
            for mentioned_user_id in mentioned_user_id_set:
                # Map users to distinct integer numbers.
                graph_size = len(id_to_node)
                mention_target_node = id_to_node.setdefault(mentioned_user_id, graph_size)

                append_user_id(mentioned_user_id)

                # Add values to the sparse matrix arrays.
                append_mention_graph_row(source_node)
                append_mention_graph_col(mention_target_node)

        ################################################################################################################
        # We are dealing with a retweet.
        ################################################################################################################
        else:
            # Extract base tweet's values.
            original_tweet = tweet["retweeted_status"]

            try:
                original_tweet_id = original_tweet["id"]
                original_tweet_user_id = original_tweet["user"]["id"]
                original_tweet_user_screen_name = original_tweet["user"]["screen_name"]
                original_tweet_user_name = original_tweet["user"]["name"]

                listed_count_raw = original_tweet["user"]["listed_count"]

                original_tweet_in_reply_to_user_id = original_tweet["in_reply_to_user_id"]
                original_tweet_in_reply_to_screen_name = original_tweet["in_reply_to_screen_name"]
                original_tweet_entities_user_mentions = original_tweet["entities"]["user_mentions"]
            except KeyError:
                continue

            # Map users to distinct integer numbers.
            graph_size = len(id_to_node)
            original_tweet_node = id_to_node.setdefault(original_tweet_user_id, graph_size)

            if listed_count_raw is None:
                id_to_listedcount[original_tweet_user_id] = 0
            else:
                id_to_listedcount[original_tweet_user_id] = int(listed_count_raw)

            # Update retweet graph.
            append_retweet_graph_row(source_node)
            append_retweet_graph_col(original_tweet_node)

            # Get mentioned user ids.
            mentioned_user_id_set = list()
            if original_tweet_in_reply_to_user_id is not None:
                mentioned_user_id_set.append(original_tweet_in_reply_to_user_id)

                id_to_name[original_tweet_in_reply_to_user_id] = original_tweet_in_reply_to_screen_name
            for user_mention in original_tweet_entities_user_mentions:
                mentioned_user_id = user_mention["id"]  # TODO: Perhaps safe extract as well.
                mentioned_user_id_set.append(mentioned_user_id)

                id_to_name[mentioned_user_id] = user_mention["screen_name"]  # TODO: Perhaps safe extract as well.

            # We remove duplicates.
            mentioned_user_id_set = set(mentioned_user_id_set)

            # Get mentioned user ids.
            retweet_mentioned_user_id_set = list()
            if original_tweet_in_reply_to_user_id is not None:
                retweet_mentioned_user_id_set.append(original_tweet_in_reply_to_user_id)

                id_to_name[original_tweet_in_reply_to_user_id] = original_tweet_in_reply_to_screen_name
            for user_mention in original_tweet_entities_user_mentions:
                mentioned_user_id = user_mention["id"]  # TODO: Perhaps safe extract as well.
                retweet_mentioned_user_id_set.append(mentioned_user_id)

                id_to_name[mentioned_user_id] = user_mention["screen_name"]  # TODO: Perhaps safe extract as well.

            # We remove duplicates.
            retweet_mentioned_user_id_set = set(retweet_mentioned_user_id_set)

            mentioned_user_id_set.update(retweet_mentioned_user_id_set)

            # Update the mention graph one-by-one.
            for mentioned_user_id in mentioned_user_id_set:
                # Map users to distinct integer numbers.
                graph_size = len(id_to_node)
                mention_target_node = id_to_node.setdefault(mentioned_user_id, graph_size)

                append_user_id(mentioned_user_id)

                # Add values to the sparse matrix arrays.
                append_mention_graph_row(source_node)
                append_mention_graph_col(mention_target_node)

            # This is the first time we deal with this tweet.
            if original_tweet_id not in tweet_id_set:
                # Update sets, lists and dictionaries.
                add_tweet_id(original_tweet_id)
                id_to_name[original_tweet_user_id] = original_tweet_user_screen_name
                id_to_username[original_tweet_user_id] = original_tweet_user_name
                append_user_id(original_tweet_user_id)

                ########################################################################################################
                # Update mention matrix.
                ########################################################################################################
                # Update the mention graph one-by-one.
                for mentioned_user_id in retweet_mentioned_user_id_set:
                    # Map users to distinct integer numbers.
                    graph_size = len(id_to_node)
                    mention_target_node = id_to_node.setdefault(mentioned_user_id, graph_size)

                    append_user_id(mentioned_user_id)

                    # Add values to the sparse matrix arrays.
                    append_mention_graph_row(original_tweet_node)
                    append_mention_graph_col(mention_target_node)
            else:
                pass

    ####################################################################################################################
    # Final steps of preprocessing tweets.
    ####################################################################################################################
    # Discard any duplicates.
    user_id_set = set(user_id_set)
    number_of_users = len(user_id_set)
    # min_number_of_users = max(user_id_set) + 1

    # Form mention graph adjacency matrix.
    mention_graph_row = np.array(mention_graph_row, dtype=np.int64)
    mention_graph_col = np.array(mention_graph_col, dtype=np.int64)
    mention_graph_data = np.ones_like(mention_graph_row, dtype=np.float64)

    mention_graph = spsp.coo_matrix((mention_graph_data, (mention_graph_row, mention_graph_col)),
                                    shape=(number_of_users, number_of_users))
    mention_graph = spsp.coo_matrix(spsp.csr_matrix(mention_graph))

    # Form retweet graph adjacency matrix.
    retweet_graph_row = np.array(retweet_graph_row, dtype=np.int64)
    retweet_graph_col = np.array(retweet_graph_col, dtype=np.int64)
    retweet_graph_data = np.ones_like(retweet_graph_row, dtype=np.float64)

    retweet_graph = spsp.coo_matrix((retweet_graph_data, (retweet_graph_row, retweet_graph_col)),
                                    shape=(number_of_users, number_of_users))
    retweet_graph = spsp.coo_matrix(spsp.csr_matrix(retweet_graph))

    node_to_id = dict(zip(id_to_node.values(), id_to_node.keys()))

    return mention_graph, retweet_graph, tweet_id_set, user_id_set, node_to_id, id_to_name, id_to_username, id_to_listedcount


def extract_connected_components(graph, connectivity_type, node_to_id):
    """
    Extract the largest connected component from a graph.

    Inputs:  - graph: An adjacency matrix in scipy sparse matrix format.
             - connectivity_type: A string that can be either: "strong" or "weak".
             - node_to_id: A map from graph node id to Twitter id, in python dictionary format.

    Outputs: - largest_connected_component: An adjacency matrix in scipy sparse matrix format.
             - new_node_to_id: A map from graph node id to Twitter id, in python dictionary format.
             - old_node_list: List of nodes from the possibly disconnected original graph.

    Raises:  - RuntimeError: If there the input graph is empty.
    """
    # Get a networkx graph.
    nx_graph = nx.from_scipy_sparse_matrix(graph, create_using=nx.DiGraph())

    # Calculate all connected components in graph.
    if connectivity_type == "weak":
        largest_connected_component_list = nxalgcom.weakly_connected_component_subgraphs(nx_graph)
    elif connectivity_type == "strong":
        largest_connected_component_list = nxalgcom.strongly_connected_component_subgraphs(nx_graph)
    else:
        print("Invalid connectivity type input.")
        raise RuntimeError

    # Handle empty graph.
    try:
        largest_connected_component = max(largest_connected_component_list, key=len)
    except ValueError:
        print("Error: Empty graph.")
        raise RuntimeError

    old_node_list = largest_connected_component.nodes()
    node_to_node = dict(zip(np.arange(len(old_node_list)), old_node_list))
    largest_connected_component = nx.to_scipy_sparse_matrix(largest_connected_component, dtype=np.float64, format="csr")

    # Make node_to_id.
    new_node_to_id = {k: node_to_id[v] for k, v in node_to_node.items()}

    return largest_connected_component, new_node_to_id, old_node_list
