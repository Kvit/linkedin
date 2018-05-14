# exploring text analysis functionality
library(RTextTools)
library(data.table)
library(readxl)
library(stringr)

#locations
profiles="Y:/linkedin/group_profiles_all.xlsx"
dir_report<-"Z:/Reports"

#---- load data -----

# load group profile data

dt_group<-setDT(read_excel(profiles))
names(dt_group)<-str_replace_all(names(dt_group), " ", "\\.")

# remove non roman alphabet characters
dt_group[, Title:=str_replace_all(Title, "[^A-z ]", "")]

# remove short and empty titles
dt_group<-dt_group[nchar(Title)>5]

# create training set
dt_train<-dt_group[!is.na(Label),.(Label, Title, Profile.url)]
# reshuffle rows in training set
dt_train<-dt_train[sample(nrow(dt_train), nrow(dt_train)), ]
rows_labeled<-nrow(dt_train)

# create predicting data
dt_pred<-dt_group[is.na(Label),.(Label, Title, Profile.url)]

# bind tables together
dt_all<-rbind(dt_train, dt_pred)
rows_all<-nrow(dt_all)

# labels to integer
labels<-as.factor(dt_all$Label)
lev=levels(labels)
dt_all$Label<-as.integer(labels)
dt_all[is.na(Label), Label:=1L]

# create matrix remove space terms
matrix<-create_matrix(dt_all$Title, language="english", removeSparseTerms = 0.995,
                         removeNumbers = T, removeStopwords = T, removePunctuation = T,
                         toLower = T, stripWhitespace = T)


#---- train models ----

# make training matrix
matrix_train<-matrix[1:rows_labeled,]
lbls<-dt_all$Label[1:rows_labeled]
rows_train=as.integer(rows_labeled*0.8)

# create training container
container<-create_container(matrix_train,  labels = lbls, trainSize = 1:rows_train, testSize =(rows_train+1):rows_labeled, virgin = F)

# train models
models <- train_models(container, algorithms=c("MAXENT","SVM","SLDA","BOOSTING","BAGGING","RF", "NNET", "TREE"))

# classify
results <- classify_models(container, models)

# make analytics
analytics <- create_analytics(container, results)
stats_label<-analytics@label_summary
stats_alg<-analytics@algorithm_summary
stats_ans<-analytics@ensemble_summary

# ----- do predictions ----

# make prediction matrix
mx_predict<-matrix[rows_labeled:rows_all,]
rows_pred<-nrow(mx_predict)

# make container
container_pred<-create_container(matrix, labels = dt_all$Label , trainSize = 1:rows_labeled,  testSize =(rows_labeled+1):rows_all, virgin = T)

# run predictions
pred <- classify_models(container_pred, models)

# get results analytics
res <- create_analytics(container_pred, pred)

final<-setDT(res@document_summary)[,.(CONSENSUS_CODE, PROBABILITY_CODE)]
final[, ConsensusLabel:=factor(CONSENSUS_CODE, labels = lev)]
final[, ProbLabel:=factor(PROBABILITY_CODE, labels = lev)]
final[,Agreed:=FALSE]
final[CONSENSUS_CODE==PROBABILITY_CODE, Agreed:=TRUE ]

final_res<-cbind(dt_all[(rows_labeled+1):rows_all,.(Profile.url, Title)], final[,-c(1:2)])


#save results

# prospects
prospects_manual<-dt_group[Label=="prospect", .(Profile.url, Title)]
prospects_ml<-final_res[Agreed==TRUE & ProbLabel=="prospect",.(Profile.url, Title)]
final_prospects<-rbind(prospects_manual, prospects_ml)
fwrite(final_prospects, file = file.path(dir_report,"url_prospects.csv"))



# recruiters
fwrite(final_res[Agreed==TRUE & ProbLabel=="recruiter"], file = file.path(dir_report,"url_recruiter.csv"))





