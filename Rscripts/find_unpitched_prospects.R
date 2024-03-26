# select target profiles from linked
library(data.table)
library(stringr)
library(quanteda)
library(tidytext)

# folders 
dir_data = "E:/OneDrive/MyDocs/Pinnacle Services/Marketing/linkedin/"


# ---- load data -----
prospects_all<-fread(file.path(dir_data, "all_prospects.csv"), check.names = T, colClasses = "char")
prospects_piched<-fread(file.path(dir_data, "piched_prospects.csv"), check.names = T, colClasses = "char")
prospects_target<-fread(file.path(dir_data, "target_prospects.csv"), check.names = T, colClasses = "char")

# ---- process data -----

prospects_merge<-merge(prospects_target[,.(First.name, Last.name, Profile.url)], prospects_piched[,.(First.name, Last.name, Full.name)], by=c("First.name", "Last.name"), all.x = T)

prospects_new<-prospects_merge[is.na(Full.name)]

#---- save results -----

fwrite(prospects_new[,.(Profile.url)], file = file.path(dir_data, "new_prospects_url.csv"))
