# update contact master xls from contact extract
library(data.table)
library(stringr)
library(quanteda)
library(readxl)
library(writexl)
require(googledrive)

# folders 
dir_data = "E:/OneDrive/MyDocs/Pinnacle Services/Marketing/linkedin/"
setwd(dir_data)
masterfile="contacts_master.xlsx"
extract_file="all_contacts_extracted.csv"

# ----- functions -----

UpdateGoogleDrive <- function( data=NULL, doc_name, match_key = NULL, replace=FALSE){

  fl<- file.path(paste0( doc_name, ".csv") )
  
  message(".. updating document on Google Dirve: /Rconfig/", doc_name)
  
  # save data if not null delete file if null
  if(!is.null(data)) {
    fwrite(data, file = fl)
  }  else {
    
    if(file.exists(fl)) file.remove(fl)
    
  }
  
  # authorize google drive toket
  if(!file.exists(".googledrive_token.rds")){
    tok <- gargle::gargle2.0_token(cache = F, scope = "https://www.googleapis.com/auth/drive")
    if(!is.null(tok)) saveRDS(tok, file = ".googledrive_token.rds")
  }
  tok <- readRDS(".googledrive_token.rds")
  drive_auth(token = tok, cache = F)
  
  # find file
  myfile<-drive_find(pattern=doc_name, type = "spreadsheet")
  if(nrow(myfile)>1) myfile <- myfile[1,]
  
  
  # if no data , download only and return data
  if (is.null(data)){
    if ( nrow(myfile) ==0  ){
      message("! Cannot find spreadsheet with name: ", doc_name)
      return(NULL)
    }
    
    drive_download(file=as_id(myfile), path =fl, overwrite = T, type = "csv" )
    gs_cont <- fread(fl, colClasses = "character")
    return(gs_cont)
  }
  
  # if no file found upload it
  if ( nrow(myfile) ==0  ) {
    
    if(is.null(data)){
      message("- File not found on Google Drive and no data to upload")
      return(NULL)
    }
    
    # upload file
    drive_upload(fl, path="Rconfig/", name = doc_name, type = "spreadsheet", overwrite = T)
    return(data)
    
  } else if ( replace ){
    # replace existing file anyway
    
    if(is.null(data)){
      message("- replacing requested but nothing to upload")
      return(NULL)
    }
    
    message("- warning: replacing without merging previous file: ", doc_name)
    drive_upload(fl, path="Rconfig/", name = doc_name, type = "spreadsheet", overwrite = T)
    
  }else {
    
    # get content from google drive
    drive_download(file=as_id(myfile), path =fl, overwrite = T, type = "csv" )
    gs_cont <- fread(fl, colClasses = "character")
    key_cont <- gs_cont[[match_key]] %>% unique()
    
    # compare keys
    key_data <- data[[match_key]] %>% unique()
    
    # return if no change
    if( all(key_data %in% key_cont) ) {
      message("- no new entries for key: ", match_key)
      return(gs_cont)
    }
    
    # update if changed
    # mergy with current list
    data<-data[,lapply(.SD, as.character)]
    data_all <- rbind(gs_cont, data, fill=TRUE) %>% unique(by= match_key )
    
    # upload file
    drive_upload(fl, path="Rconfig/", name = doc_name, type = "spreadsheet", overwrite = T)
    
    return(data_all)
  }
  
  return(NULL)
  
}


# ---- load data -----

# contact master
# contact_master<-read_xlsx(masterfile) %>% setDT()

# add to google drive
# master <- UpdateGoogleDrive(data = contact_master, doc_name = "LinkedInContacts", match_key = "Profile.url")


# contact extract
contacts_extract <-fread(extract_file, 
                         sep = ",",
                         check.names = T,
                         colClasses = "character",
                         fill = T,
                         header = T)

# ----- process data -----

# get current sheet
master <- UpdateGoogleDrive(doc_name = "LinkedInContacts", match_key = "Profile.url")

# new master from extract
# master columns
cols_master <- names(master) 

# new master
cols_new_master <- names(contacts_extract) %in% cols_master %>% which()

new_master<-contacts_extract[,..cols_new_master] %>%
  merge(master[,.(Profile.url, Group, Date)], by= "Profile.url", all.x = T)

new_master[is.na(Group), Group:="NEW"]

#!dev
stop("reconsile manually, e.g delete google doc")

# add new entries
master <- UpdateGoogleDrive(data = new_master, doc_name = "LinkedInContacts", match_key = "Profile.url")


