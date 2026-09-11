# reconsile linkedin contacts with quanteda
library(data.table)
library(stringr)
library(quanteda)
library(readxl)
library(writexl)

# folders 
dir_data = "E:/OneDrive/MyDocs/Pinnacle Services/Marketing/linkedin/"
setwd(dir_data)

# ---- load data -----
# all_contacts<-fread(file.path(dir_data, "all_contacts_extracted.csv"), colClasses = "char", sep = ";", header = F, quote = "")
all_contacts<-read_excel("all_contacts_extracted.xlsx", col_types = "text") %>% setDT()
names(all_contacts)<-str_replace_all(names(all_contacts), " ", ".")

all_prospects<-fread(file.path(dir_data, "all_prospects.csv"), check.names = T, colClasses = "char")
pitched_prospects<-fread(file.path(dir_data, "piched_prospects.csv"), check.names = T, colClasses = "char")
all_rcm<-fread(file.path(dir_data, "all_rcm.csv"), check.names = T, colClasses = "char")
all_hr<-fread(file.path(dir_data, "all_recruiters.csv"), check.names = T, colClasses = "char")

#!tmp
# names_all<-data.table(Field=names(all_contacts) )
# fwrite(names_all, file = file.path(dir_data, "field_names.csv"))

#read names
names_all<-fread(file.path(dir_data, "field_names.csv"), check.names = T, colClasses = "char")

# ---- process data -----

# join first and last name
all_contacts[, FirstLast:=paste0(First.name, Last.name)]
all_contacts<-unique(all_contacts, by="FirstLast")

# join names on lists
all_prospects[, FirstLast:=paste0(First.name, Last.name)]
pitched_prospects[, FirstLast:=paste0(First.name, Last.name)]
all_rcm[, FirstLast:=paste0(First.name, Last.name)]
all_hr[, FirstLast:=paste0(First.name, Last.name)]

# create clean master list
cols<-c(names_all[USE=="y", Field], "FirstLast")
contacts<-all_contacts[, ..cols]

# create categories
tags<- merge(all_prospects[,.(FirstLast, Prospect="Y")], pitched_prospects[,.(FirstLast, Pitched="Y")], by="FirstLast", all=T) %>%
  merge(all_rcm[,.(FirstLast, RCM="Y")], by="FirstLast",  all=T) %>% 
  merge(all_hr[,.(FirstLast, HR="Y")], by="FirstLast",  all=T) %>%
  unique(by="FirstLast")

# create group
tags[, Group:="Other"]
tags[Prospect=="Y" | Pitched=="Y", Group:="Prospect"]
tags[RCM=="Y", Group:="RCM"]
tags[HR=="Y", Group:="HR"]

# create skills
skills_all<-all_contacts[,.(FirstLast, Skills)]

# process skills set
skill_top=vector()
i=1

message("processing skills..")

for (i in 1:nrow(skills_all)) {

l1<-str_replace_all(skills_all$Skills[i], ' ', '') %>% str_replace_all('\"', '') %>% str_split( ",")
l2<-l1[[1]]

# parse skill counts
v_skill=vector()
v_count=vector()
k=1
for (k in 1:length(l2)) {
  l3<-str_split_fixed(l2[k], ":",2)
  v_skill<-c(v_skill, l3[1])
  v_count<-c(v_count, l3[2])
}

# keep top 5 skills
skills_set<-data.table(v_skill, Cnt=as.integer(v_count) )[!is.na(Cnt)]
setorder(skills_set, -Cnt)
skill_top[i]<-skills_set[1:5, paste(v_skill, collapse = " ")]

}

# assemble skills
skills_res<-data.table(FirstLast=skills_all$FirstLast, TopSkills=skill_top)[FirstLast!=""] %>% unique(by="FirstLast")

# add skills
contacts2<-merge(contacts, skills_res, by="FirstLast") %>% 
  merge(tags, by="FirstLast", all.x = T)

contacts2[is.na(Group), Group:="Unk"]

# create text for string analysis 
contacts2[, Text:=paste(Title, Organization.1, TopSkills ) ]

# remove other
contacts3<-contacts2[Group!="Other"]

# make corpus
corp<-corpus(contacts3[,.(id=FirstLast, text = Text, Group)])

# create dfm
dfm_comp<-dfm(corp, remove_punct = T, remove_symbols = T , remove = stopwords('en')) 


#TODO fix profile picture

# assemble final results
contacts_xl<-contacts2[,.(Full.name, Title, Organization.1, Group, Profile.url)]

#---- save results -----

write_xlsx(contacts_xl, "contacts_unsorted.xlsx")

# ----- backup -----

# # wordfish model
# dfm2<-dfm_trim(dfm_comp, min_termfreq=5)
# mod_wf<-textmodel_wordfish(dfm2, dispersion_level="overall", sparse = T)
# summary(mod_wf)
# 
# # labels
# lbl<-paste(docvars(dfm2, "id"), docvars(dfm2, "Group") )
# 
# # chart model
# textplot_scale1d(mod_wf, doclabels = lbl, groups = docvars(dfm2, "Group"))
# 
# # group dfm 
# dfm_grp<-dfm_group(dfm_comp, groups = docvars(corp, "Group"))
# key = textstat_keyness(dfm_grp)
# textplot_keyness(key)
