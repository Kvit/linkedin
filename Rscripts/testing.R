# exploring text analysis functionality
library(text2vec)
library(data.table)


#---- load data -----

# load group member data
fl="Y:/linkedin/group_profiles_all.txt"
dt_group<-fread(fl, sep = ";", check.names = T)

# test
data("movie_review")
dt_t<-movie_review$review[1:10]
ids = movie_review$id[1:10]
it = itoken(dt_t, tolower, word_tokenizer, n_chunks = 10, ids = ids)

# tokenize text
txt<-dt_group$Title
it<-itoken(txt, tolower, word_tokenizer)

# collocation model
model = Collocations$new()
model$fit(it, n_iter = 5)
model$collocation_stat
