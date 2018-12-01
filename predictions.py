from py2neo import Graph
import pandas as pd

from pyspark.ml import Pipeline
from pyspark.ml.classification import RandomForestClassifier
from pyspark.ml.feature import StringIndexer, VectorAssembler
from pyspark.ml.evaluation import BinaryClassificationEvaluator

from pyspark.sql.types import *
from pyspark.sql import functions as F

from collections import Counter


def preprocess_df(df):
    processed_df = df.copy()
    zero = Counter(processed_df.label.values)[0]
    un = Counter(processed_df.label.values)[1]
    n = zero - un
    processed_df['label'] = processed_df['label'].astype('category')
    processed_df = processed_df.drop(
        processed_df[processed_df.label == 0].sample(n=n, random_state=1).index)
    return processed_df.sample(frac=1)


def evaluate(predictions):
    evaluator = BinaryClassificationEvaluator()
    accuracy = evaluator.evaluate(predictions)
    tp = predictions[(predictions.label == 1) & (predictions.prediction == 1)].count()
    tn = predictions[(predictions.label == 0) & (predictions.prediction == 0)].count()
    fp = predictions[(predictions.label == 0) & (predictions.prediction == 1)].count()
    fn = predictions[(predictions.label == 1) & (predictions.prediction == 0)].count()
    recall = float(tp) / (tp + fn)
    precision = float(tp) / (tp + fp)
    return pd.DataFrame({
        "Measure": ["Accuracy", "Precision", "Recall"],
        "Score": [accuracy, precision, recall]
    })


# end::evaluate-function[]


# tag::prep-function[]
def create_pipeline(fields):
    assembler = VectorAssembler(inputCols=fields, outputCol="features")
    rf = RandomForestClassifier(labelCol="label", featuresCol="features",
                                numTrees=30, maxDepth=10)
    return Pipeline(stages=[assembler, rf])


# tag::py2neo[]
graph = Graph("bolt://localhost:7687", auth=("neo4j", "neo"))
# end::py2neo[]

train_existing_links = graph.run("""
MATCH (author:Author)-[:CO_AUTHOR_EARLY]-(other:Author)
RETURN id(author) AS node1, id(other) AS node2, 1 AS label
""").to_data_frame()

train_missing_links = graph.run("""
MATCH (author:Author)
WHERE (author)-[:CO_AUTHOR_EARLY]-()
MATCH (author)-[:CO_AUTHOR_EARLY*2]-(other)
WHERE not((author)-[:CO_AUTHOR_EARLY]-(other))
RETURN id(author) AS node1, id(other) AS node2, 0 AS label
""").to_data_frame()

training_df = train_missing_links.append(train_existing_links, ignore_index=True)
training_df = preprocess_df(training_df)
training_data = spark.createDataFrame(training_df)

test_existing_links = graph.run("""
MATCH (author:Author)-[:CO_AUTHOR_LATE]-(other:Author)
RETURN id(author) AS node1, id(other) AS node2, 1 AS label
""").to_data_frame()

test_missing_links = graph.run("""
MATCH (author:Author)
WHERE (author)-[:CO_AUTHOR_LATE]-()
MATCH (author)-[:CO_AUTHOR*2]-(other)
WHERE not((author)-[:CO_AUTHOR]-(other))
RETURN id(author) AS node1, id(other) AS node2, 0 AS label
""").to_data_frame()

test_df = test_missing_links.append(test_existing_links, ignore_index=True)
test_df = preprocess_df(test_df)
test_data = spark.createDataFrame(test_df)


# Build features

def apply_training_features(training_data):
    training_features_query = """
    UNWIND $pairs AS pair
    MATCH (p1) WHERE id(p1) = pair.node1
    MATCH (p2) WHERE id(p2) = pair.node2
    RETURN pair.node1 AS node1, 
           pair.node2 AS node2,
           algo.similarity.jaccard([(p1)-[:CO_AUTHOR_EARLY]-> (a) | id(a)], [(p2)-[:CO_AUTHOR_EARLY]-> (a) | id(a)]) AS jaccard,
           apoc.coll.min([size((p1)-[:CO_AUTHOR_EARLY]-()), size((p2)-[:CO_AUTHOR_EARLY]-())]) AS minNeighbours,
           apoc.coll.max([size((p1)-[:CO_AUTHOR_EARLY]-()), size((p2)-[:CO_AUTHOR_EARLY]-())]) AS maxNeighbours,
           size([(p1)-[:CO_AUTHOR_EARLY]-(a)-[:CO_AUTHOR_EARLY]-(p2) | a]) AS commonAuthors,
           size((p1)-[:CO_AUTHOR_EARLY]-()) * size((p2)-[:CO_AUTHOR_EARLY]-()) AS prefAttachment,
           size(apoc.coll.toSet([(p1)-[:CO_AUTHOR_EARLY]->(a) | id(a)] + [(p2)-[:CO_AUTHOR_EARLY]->(a) | id(a)])) AS totalNeighbours,
           size(apoc.coll.toSet([(p1)-[:CO_AUTHOR_EARLY]->()-[:CO_AUTHOR_EARLY]->(other) | id(other)] + [(p2)-[:CO_AUTHOR_EARLY]->()-[:CO_AUTHOR_EARLY]->(other) | id(other)])) AS neighboursMeasure,
           apoc.coll.min([p1.trianglesTrain, p2.trianglesTrain]) AS minTriangles,
           apoc.coll.max([p1.trianglesTrain, p2.trianglesTrain]) AS maxTriangles,
           apoc.coll.min([p1.coefficientTrain, p2.coefficientTrain]) AS minCoefficient,
           apoc.coll.max([p1.coefficientTrain, p2.coefficientTrain]) AS maxCoefficient,
           apoc.coll.min([p1.pagerankTrain, p2.pagerankTrain]) AS minPageRank,
           apoc.coll.max([p1.pagerankTrain, p2.pagerankTrain]) AS maxPageRank,
           CASE WHEN p1.partitionTrain = p2.partitionTrain THEN 1 ELSE 0 END AS samePartition,
           CASE WHEN p1.louvainTrain = p2.louvainTrain THEN 1 ELSE 0 END AS sameLouvain
    """
    params = {"pairs": [{"node1": row["node1"], "node2": row["node2"]}
                        for row in training_data.collect()]}
    training_features = spark.createDataFrame(
        graph.run(training_features_query, params).to_data_frame())
    return training_data.join(training_features, ["node1", "node2"])


def apply_test_features(test_data):
    test_features_query = """
    UNWIND $pairs AS pair
    MATCH (p1) WHERE id(p1) = pair.node1
    MATCH (p2) WHERE id(p2) = pair.node2
    RETURN pair.node1 AS node1, 
           pair.node2 AS node2,
           algo.similarity.jaccard([(p1)-[:CO_AUTHOR]-> (a) | id(a)], [(p2)-[:CO_AUTHOR]-> (a) | id(a)]) AS jaccard,
           apoc.coll.min([size((p1)-[:CO_AUTHOR]-()), size((p2)-[:CO_AUTHOR]-())]) AS minNeighbours,
           apoc.coll.max([size((p1)-[:CO_AUTHOR]-()), size((p2)-[:CO_AUTHOR]-())]) AS maxNeighbours,
           size([(p1)-[:CO_AUTHOR]-(a)-[:CO_AUTHOR]-(p2) | a]) AS commonAuthors,
           size(apoc.coll.toSet([(p1)-[:CO_AUTHOR]->(a) | id(a)] + [(p2)-[:CO_AUTHOR]->(a) | id(a)])) AS totalNeighbours,
           size(apoc.coll.toSet([(p1)-[:CO_AUTHOR]->()-[:CO_AUTHOR]->(other) | id(other)] + [(p2)-[:CO_AUTHOR]->()-[:CO_AUTHOR]->(other) | id(other)])) AS neighboursMeasure,
           size((p1)-[:CO_AUTHOR]-()) * size((p2)-[:CO_AUTHOR]-()) AS prefAttachment,
           apoc.coll.min([p1.trianglesTest, p2.trianglesTest]) AS minTriangles,
           apoc.coll.max([p1.trianglesTest, p2.trianglesTest]) AS maxTriangles,
           apoc.coll.min([p1.coefficientTest, p2.coefficientTest]) AS minCoefficient,
           apoc.coll.max([p1.coefficientTest, p2.coefficientTest]) AS maxCoefficient,
           apoc.coll.min([p1.pagerankTest, p2.pagerankTest]) AS minPageRank,
           apoc.coll.max([p1.pagerankTest, p2.pagerankTest]) AS maxPageRank,
           CASE WHEN p1.partitionTest = p2.partitionTest THEN 1 ELSE 0 END AS samePartition,
           CASE WHEN p1.louvainTest = p2.louvainTest THEN 1 ELSE 0 END AS sameLouvain
    """
    params = {"pairs": [{"node1": row["node1"], "node2": row["node2"]}
                        for row in test_data.collect()]}
    test_features = spark.createDataFrame(
        graph.run(test_features_query, params).to_data_frame())
    return test_data.join(test_features, ["node1", "node2"])


rf_model = model.stages[-1]
feature_importance = pd.DataFrame({"Feature": fields, "Importance": rf_model.featureImportances})
print(feature_importance)



def train_and_evaluate_model(fields, training_data, test_data):
    pipeline = create_pipeline(fields)
    model = pipeline.fit(training_data)
    predictions = model.transform(test_data)
    print(evaluate(predictions))
    rf_model = model.stages[-1]
    print(pd.DataFrame({"Feature": fields, "Importance": rf_model.featureImportances}))


training_data = apply_training_features(training_data)
test_data = apply_test_features(test_data)

train_and_evaluate_model(["commonAuthors"], training_data, test_data)
#      Measure     Score
# 0   Accuracy  0.745353
# 1  Precision  0.757454
# 2     Recall  0.503539

train_and_evaluate_model(["commonAuthors", "prefAttachment"], training_data, test_data)

#      Measure     Score
# 0   Accuracy  0.805149
# 1  Precision  0.781847
# 2     Recall  0.611137

train_and_evaluate_model(["commonAuthors", "sameLouvain"], training_data, test_data)

#      Measure     Score
# 0   Accuracy  0.825818
# 1  Precision  0.744264
# 2     Recall  0.801164

train_and_evaluate_model(["commonAuthors", "prefAttachment", "sameLouvain"], training_data, test_data)

#      Measure     Score
# 0   Accuracy  0.850711
# 1  Precision  0.802288
# 2     Recall  0.750039

train_and_evaluate_model(["commonAuthors", "prefAttachment", "sameLouvain", "minTriangles", "maxTriangles"], training_data, test_data)

#      Measure     Score
# 0   Accuracy  0.917028
# 1  Precision  0.847259
# 2     Recall  0.774422

train_and_evaluate_model(["commonAuthors", "prefAttachment", "sameLouvain", "minTriangles", "maxTriangles", "minCoefficient", "maxCoefficient"], training_data, test_data)

#      Measure     Score
# 0   Accuracy  0.924974
# 1  Precision  0.855788
# 2     Recall  0.780871


train_and_evaluate_model(["commonAuthors", "prefAttachment", "sameLouvain", "minTriangles", "maxTriangles", "minCoefficient", "maxCoefficient", "minPageRank", "maxPageRank"], training_data, test_data)

#      Measure     Score
# 0   Accuracy  0.923085
# 1  Precision  0.850103
# 2     Recall  0.778826

train_and_evaluate_model(["commonAuthors", "prefAttachment", "sameLouvain", "minTriangles", "maxTriangles", "minCoefficient", "maxCoefficient", "samePartition"], training_data, test_data)

#      Measure     Score
# 0   Accuracy  0.918728
# 1  Precision  0.848956
# 2     Recall  0.799276

train_and_evaluate_model(["commonAuthors", "prefAttachment", "sameLouvain", "minTriangles", "maxTriangles", "minCoefficient", "maxCoefficient", "samePartition", "jaccard"], training_data, test_data)

#      Measure     Score
# 0   Accuracy  0.918076
# 1  Precision  0.848824
# 2     Recall  0.800220


train_and_evaluate_model(["commonAuthors", "totalNeighbours", "prefAttachment", "sameLouvain", "minTriangles", "maxTriangles", "minCoefficient", "maxCoefficient", "samePartition", "jaccard"], training_data, test_data)

#      Measure     Score
# 0   Accuracy  0.923895
# 1  Precision  0.851459
# 2     Recall  0.807928

train_and_evaluate_model(["commonAuthors", "totalNeighbours", "neighboursMeasure", "prefAttachment", "sameLouvain", "minTriangles", "maxTriangles", "minCoefficient", "maxCoefficient", "samePartition", "jaccard"], training_data, test_data)

#      Measure     Score
# 0   Accuracy  0.923732
# 1  Precision  0.855899
# 2     Recall  0.803996

train_and_evaluate_model(["commonAuthors", "minNeighbours", "maxNeighbours", "totalNeighbours", "neighboursMeasure", "prefAttachment", "sameLouvain", "minTriangles", "maxTriangles", "minCoefficient", "maxCoefficient", "samePartition", "jaccard"], training_data, test_data)

#      Measure     Score
# 0   Accuracy  0.921881
# 1  Precision  0.849239
# 2     Recall  0.794400
