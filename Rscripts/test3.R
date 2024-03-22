# exploring text analysis functionality
library(RTextTools)
library(data.table)
library(readxl)
library(stringr)


#---- load data -----

# load group profile data
fl="Y:/linkedin/group_profiles_all.xlsx"
dt_group<-setDT(read_excel(fl))
names(dt_group)<-str_replace_all(names(dt_group), " ", "\\.")

# remove non roman alphabet characters
dt_group[, Title:=str_replace_all(Title, "[^A-z ]", "")]

# remov short and empty docs
dt_group<-dt_group[nchar(Title)>5]

# create training set
dt_train<-dt_group[!is.na(Label),.(Label, Title)]%>%setDF()
# reshuffle rows in training set
dt_train<-dt_train[sample(nrow(dt_train), nrow(dt_train)), ]

# create predicting data
dt_pred<-dt_group[is.na(Label),.(Title)]%>%setDF()

#---- train models ----

# create matrix remove space terms
matrix_train<-create_matrix(dt_train$Title, language="english", removeSparseTerms = 0.995,
                         removeNumbers = T, removeStopwords = T, removePunctuation = T,
                         toLower = T, stripWhitespace = T, weighting=tm::weightTfIdf)


# create container
labels<-as.factor(dt_train$Label)
dt_train$Topic.Code<-as.integer(labels)
n_docs=matrix_train$nrow
n_train=round(n_docs)
container<-create_container(matrix_train, labels = dt_train$Topic.Code, trainSize = 1:100,  testSize = 101:n_docs, virgin = F)

# train models
models <- train_models(container, algorithms=c("MAXENT","SVM","SLDA","BOOSTING","BAGGING","RF", "NNET", "TREE"))

# classify
results <- classify_models(container, models)

# make analytics
analytics <- create_analytics(container, results)
summary(analytics)

# ----- do predictions ----

# make matrix
mx_predict<-create_matrix(dt_train$Title, originalMatrix=matrix_train, weighting=tm::weightTfIdf)
                          
                          # ,  language="english",  removeSparseTerms = 0.995,
                          # removeNumbers = T, removeStopwords = T, removePunctuation = T,
                          # toLower = T, stripWhitespace = T)



