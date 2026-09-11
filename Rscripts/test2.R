# exploring text analysis functionality
library(quanteda)
library(data.table)


#---- load data -----

# load group member data
fl="Y:/linkedin/group_profiles_all.txt"
dt_group<-fread(fl, sep = ";", check.names = T)


# create corups
dt_in<-dt_group[,.(Text=paste(Title, Company, Position), id)]
cor<-corpus(setDF(dt_in), text_field = "Text", docid_field = "id")

summary(cor)

# build dfm 
dfm1 <- dfm(cor, remove = stopwords("english"), tolower = TRUE, stem = FALSE, remove_punct = TRUE)

# visualize dfm
features<-topfeatures(dfm1, 1000)
stat<-data.table(Features=names(features), Count = features)[Count>20]
textplot_wordcloud(dfm1, min.freq = 100, random.order = FALSE, random.color = TRUE,

                                      rot.per = .25, 
                   colors = RColorBrewer::brewer.pal(8,"Dark2"))

# collocations
coloc<-textstat_collocations(cor, size = 2, min_count = 5, smoothing = 0.5, tolower = TRUE)

