# select target profiles from linked it with quanteda
library(stringr)
library(quanteda)
library(data.table)

# folders 
dir_reports='Z:/Reports' 
dir_data = "L:"



# data files
input="L:/reimb_group_autovisit_min.csv"
input[2] ="L:/lab_managers_group_autovisit_min.csv"
input[3] = "L:/exec_group_autovisit_min.csv"



# ---- load data -----
dt_in=list()
for (i in 1: length(input)){
  dt_in[[i]] <-fread(input[i], sep = ";", check.names = T)
}

# bind
dt_all<-rbindlist(dt_in)%>%unique(by="id")

# ---- process data -----

# remove empty position or company
dt<-dt_all[Position!="" & Company != ""]

# make full description
dt[,FullDesc:=tolower(paste(Title, Position, Company))]

# construct corpus
corp<-corpus(dt, text_field = "FullDesc", docid_field = "id")

# tokenize
toks<-tokens(corp, remove_numbers = TRUE, remove_punct = TRUE,
             remove_symbols = TRUE, remove_separators = TRUE)

# remove stopwords
toks<-tokens_select(toks, stopwords('en'), selection = 'remove', padding = FALSE)

# collocation analysis
coloc<-textstat_collocations(toks, min_count = 20)%>%setDT()


# compound high colocation tokens
toks_comp<-tokens_compound(toks, phrase(coloc$collocation[coloc$z > 4]))

# make dfm
dfm1<-dfm(toks_comp)%>%dfm_trim(min_count = 10)
features<-topfeatures(dfm1, 1000)
stat<-data.table(Features=names(features), Count = features)

# read mapped features
fl=file.path(dir_data, "linkedin_features_mapped.csv")
if (file.exists(fl))  {
  
  dic_features= fread(fl)
  stat<-merge(stat, dic_features[, .(Features, Level, Function)], by = "Features", all.x = T)
  setorder(stat, -Count)

} else message(" ! updated features files does not exist")

# save features to csv for manual mapping
fl=file.path(dir_reports, "linkedin_features.csv")
fwrite(stat, file = fl)
message("features saved to: ", fl)

#--- select docs based on mappings ---- 

# select levels
levls=unique(dic_features[Level!="",Level])

lvl=levls[1] #!dev
for (lvl in levls) {
selection = dic_features[Level==lvl, Features ]
message("Level = ", lvl, " | Features = ", paste(selection, collapse = ", "))

# subset
dfm_sel<-dfm_keep(dfm1, selection)
mx_sel<-convert(dfm_sel, to="matrix")
row_sum<-rowSums(mx_sel)
dt_sel<-data.table(id=rownames(mx_sel), Count=row_sum)[Count>0, id]
dt[id %chin% dt_sel, Level:=lvl]

#test dt
dt_sel<-convert(dfm_sel, to="data.frame")
dt_test<-merge(dt[,.(id, FullDesc)], dt_sel, by.x = "id", by.y = "document")

}

# select function
funcs<-unique(dic_features[Function!="",Function])

fn<-funcs[1] #!dev
for (fn in funcs){
selection = dic_features[Function==fn, Features ]

# subset
dfm_sel<-dfm_keep(dfm1, selection)
mx_sel<-convert(dfm_sel, to="matrix")
row_sum<-rowSums(mx_sel)
dt_sel<-data.table(id=rownames(mx_sel), Count=row_sum)[Count>0, id]
dt[id %chin% dt_sel, Function:=fn]
}

# check non classified
dt_na<-dt[is.na(Level) & is.na(Function), .(Level, Function, FullDesc)]


#---- save results -----



# distribution and clustering
# 
# # find distances
# dist <- textstat_dist(dfm_weight(dfm1,'prop'), margin = "documents")
# clust<-hclust(dist)
# 
# # cut tree into groups
# groups <- cutree(clust, h=1) # at level
# clust_groups<-data.table(id=names(groups), Cluster=groups)
# print(clust_groups[,.N, by=Cluster])
# 
# 
# # add cluster to original data
# res<-merge(clust_groups, dt, by="id")


