# select target profiles from linked it with quanteda
library(data.table)
library(stringr)
library(quanteda)
library(tidytext)

# folders 
dir_data = "E:/OneDrive/MyDocs/Pinnacle Services/Marketing/linkedin/profile_extracts"
dir_reports="E:/OneDrive/MyDocs/Pinnacle Services/Marketing/linkedin"



# ---- load data -----

files<-list.files(dir_data, full.names = T)

dt_in=list()
i=1
for (fl in files){
  dt_in[[i]] <-fread(fl, check.names = T)
  i=i+1
}

# bind
dt_all<-rbindlist(dt_in, fill = T)%>%unique(by="id")

names<-data.table(Names=names(dt_all))

# ---- process data -----

dt<-dt_all[Relationship>1 ,.(Organization.1, Organization.Title.1 , Organization.Description.1, Title, Summary, id)]

# make full description
dt[,FullDesc:=tolower(paste(Organization.1, Organization.Title.1 , Organization.Description.1, Title, Summary))]
dt[,FullDesc:=str_replace_all(FullDesc, "na|show|summary", "")]

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
dfm1<-dfm(toks_comp)%>%dfm_trim(min_termfreq =  10)


# count feature frequency
features=topfeatures(dfm1, n = 1000, decreasing = TRUE, scheme = "count")

#----- map features to categories -----

# read mapped features
fl=file.path(dir_reports, "linkedin_features_mapped.csv")
dic_features<-fread(fl)

# merge stat with feature mapping
stat<-data.table(Features=names(features), Count = features)%>%
  merge(dic_features[, .(Features, Level, Function)], by = "Features", all.x = T)

setorder(stat, -Count)

# save features to csv for manual mapping
fl=file.path(dir_reports, "linkedin_features.csv")
fwrite(stat, file = fl)
message("features saved to: ", fl)

#---- subset by dicionary ----

# level dictionary
lev_dt<-stat[Level!="", .(Dic=list(Features)), by=Level]
lev<-as.list(lev_dt$Dic)
names(lev)<-lev_dt$Level
dic_lev<-dictionary(lev)

# levels data.table
dt_level<-tidy( dfm_lookup(dfm1, dic_lev) )%>%setDT()

# function dictionary
fun_dt<-stat[Function!="", .(Dic=list(Features)), by=Function]
fun<-as.list(fun_dt$Dic)
names(fun)<-fun_dt$Function
dic_fun<-dictionary(fun)

# function data.table
dt_fun<-tidy( dfm_lookup(dfm1, dic_fun) )%>%setDT()


#----- make target lists ---- 

id_sales<-dt_fun[term=="sales",.(id=document)]%>%unique()
id_consultant<-dt_fun[term=="consulting",.(id=document)]%>%unique()

# get lab ids exlcuding sales and consultants
id_lab<-dt_fun[term=="lab",.(scoreL=sum(count)), by=.(id=document)][!id_consultant, on="id"][!id_sales, on="id"]

# levels
id_exec<-dt_level[term %chin% c("cxo", "vp"),.(scoreF=sum(count)), by=.(id=document)]
id_director<-dt_level[term %chin% c("director"),.(scoreF=sum(count)), by=.(id=document)]

# combine function and level for lab iDs
res_id_lab<-unique(rbind(id_exec, id_director))%>%merge(id_lab, by="id")

# get score
res_id_lab[,Score:=scoreL+scoreF][,c("scoreF", "scoreL"):=NULL]

# get URL list
res_url_lab<-dt_all[res_id_lab, on="id",.(Profile.url, Organization.Title.1, Organization.1, Organization.Description.1, Score)][order(-Score)]


#---- save results -----

fl<-file.path(dir_reports, 'target_url_lab.csv')
fwrite(res_url_lab[,.(Profile.url)], fl)
