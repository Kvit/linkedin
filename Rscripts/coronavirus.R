# coronavirus analysis
library(data.table)
library(magrittr)
library(googleCloudStorageR)

selected_countries = c("Italy", "Germany", "United.States", "United.Kingdom", "Sweden", "Russia" )

# ---- load data -----

# us state population
state_pop <- fread("nst-est2019-alldata.csv") 
state_pop <- state_pop[,.(fips = STATE, population = POPESTIMATE2019)]

# countries

# load new case data
new_cases <- read.csv("https://covid.ourworldindata.org/data/ecdc/new_cases.csv", stringsAsFactors=F) %>% setDT()
new_cases[, date:=as.Date(date)]

# load new cases from NY times
cases_state <- fread("https://raw.githubusercontent.com/nytimes/covid-19-data/master/us-states.csv") 
cases_state[,date:=as.Date(date)]

target_countries = data.table(
country = names(new_cases)[which(lapply(new_cases, class) %in% c( "integer", "numeric") )],
include = 0L
)

i=1
for (i in 1:nrow(target_countries)){
  country_in = target_countries$country[i]
  if(country_in=="World") next
  max_daily_cases = max(new_cases[[country_in]], na.rm = T)
  if (max_daily_cases > 200) target_countries$include[i]=max_daily_cases
  
}


# download population by country
country_pop <- fread("world_population.csv", skip = 4, header = T, check.names = T)[,.(Country.Name, population=X2018)]
country_pop[,country:=make.names(country_pop$Country.Name)]
country_pop[,population_m := round( population/1000000)]

# match some countries manually
country_pop[Country.Name %like% "Korea, Rep.", country:="South.Korea"]
country_pop[Country.Name %like% "Iran", country:="Iran"]
country_pop[Country.Name %like% "Russian", country:="Russia"]


# keep only included countries
target_res<- target_countries[include>0] %>% 
  merge(country_pop[,.(country, population_m)], by="country", all.x = T) %>%
  setorder(-include)

# limit by top 10
#target_res <- target_res[1:10]

target_res[, max_daily_cases_per_m := round(include/population_m,1)]

# assemble case stats
res = list()
i=1
n_target = nrow(target_res)

for(i in 1:n_target){
dt_in = target_res[i]
country_in = dt_in$country

cols=c("date", country_in)
country_dt<-new_cases[,..cols]
country_dt[, country:=country_in]
setnames(country_dt, country_in, "daily_cases")

# remove na
country_dt <- country_dt[!is.na(daily_cases)]

# keep at least 8 days
if(nrow(country_dt)<8) next

# moving average 7 days
setorder(country_dt, date)
country_dt[,daily_cases_ma3:= round(frollmean(daily_cases,7, fill = 0))]

# first day over 100 cases
first_day = country_dt[daily_cases>200, which=T][1]
n=nrow(country_dt)

# limit data
country_dt <- country_dt[first_day:n]
n1=nrow(country_dt)

# complete data 
country_dt[,day :=seq_len(n1)]

country_dt$population_m <- dt_in$population_m
res[[i]] <- country_dt
}

final_res <- rbindlist(res)

# plug us data manually
final_res[country=="United.States" & date== as.Date("2020-03-15"), daily_cases:=777]
final_res[country=="United.States" & date== as.Date("2020-03-16"), daily_cases:=832]
final_res[country=="United.States" & date== as.Date("2020-03-17"), daily_cases:=3503]
# final_res[country=="United.States" & date== as.Date("2020-03-18"), daily_cases:=3536]
# final_res[country=="United.States" & date== as.Date("2020-03-19"), daily_cases:=7087]


# Italy manual plug
final_res[country=="Italy" & date== as.Date("2020-03-15"), daily_cases:=3590]

# calculate cases per million
final_res[, daily_cases_per_m := round(daily_cases/population_m,1)]
final_res[, daily_cases_per_m_ma3 := round(daily_cases_ma3/population_m,1)]

# keep only seleced countries in charts
final_res[,chart:= 0L]
final_res[country %chin% selected_countries, chart:=1L]

# order results
setorder(final_res, country, date)

# cumulative sum
final_res[,total_cases:=cumsum(daily_cases), by = .(country)]
final_res[,total_cases_per_m:= round(total_cases/population_m)]

# remove na
final_res<-final_res[!is.na(daily_cases_per_m)]

# ---- inf rate countries ----


# infection rate by linear regression
i=1
inf_r = list()
for (co in unique(final_res$country) ){
  
  dt_st<-final_res[country==co] %>% setorder(date)
  
  # skip less than 5 days
  if(nrow(dt_st)<5) next
  
  # keep last 8 days
  dt_st <- tail(dt_st, 8)
  
  # liear model
  lin_mod <- lm(total_cases_per_m ~ day, data=dt_st)
  
  inf_r[[i]] = data.table(
    country = dt_st$country[1],
    cases_per_m = last(dt_st$total_cases_per_m) %>% round(),
    infection_rate_per_m = lin_mod$coefficients[2] %>% round(1)
  )
  i = i + 1
}

# aggregate
infection_rate_countries = rbindlist(inf_r)


# ---- state data -----

state_pop[, population_m := round(population/1000000,1) ]

# sort 
setorder(cases_state, fips, date)

state_fips <- unique(cases_state$fips)
n = length(state_fips)

# process state data 
state_list <- list()
i=1
for (i in 1:n){
  
  this_fips <- state_fips[i]
  
  # state data
  st_dt = cases_state[fips==this_fips]
  
  # skip sates below 100 cases
  if (max(st_dt$cases)< 101) next
  
  # skip states less than 8 days
  if(nrow(st_dt) < 8 ) next
  
  # daliy case
  daily_cases <- vector()
  for(c in nrow(st_dt):2) daily_cases[c] = st_dt$cases[c] - st_dt$cases[c-1]
  daily_cases[1]=0
  
  st_dt$daily_cases <- daily_cases
  
  # moving average 7 days
  setorder(st_dt, date)
  st_dt[,daily_cases_ma3:= round(frollmean(daily_cases,7, fill = 0))]
  
  # first day over 100 cases
  first_day = st_dt[daily_cases>100, which=T][1]
  if(is.na(first_day)) next
  n_max=nrow(st_dt)
  
  # limit data
  st_dt <- st_dt[first_day:n_max]
  n1=nrow(st_dt)
  
  if(n1<2) next
  
  # get epidemic date
  st_dt[,day :=seq_len(n1)]
  
  st_dt$population_m <- state_pop[fips==this_fips, population_m]
  

  state_list[[i]] <- st_dt
  
}

# aggregate
res_state <- rbindlist(state_list)

# scale per mil
res_state[,daily_cases_per_m:= round(daily_cases/population_m, 1)]
res_state[,daily_cases_per_m_ma3:= round(daily_cases_ma3/population_m, 1)]
res_state[,total_cases_per_m:= round(cases/population_m, 1)]

# mark selected states
st_sum <- res_state[, .(total_cases = max(total_cases_per_m)), by=fips][order(-total_cases)]

# top 3 states
selected_sates <- st_sum$fips[1:3]

# add TX, FL, CA
add_states <- c(48,6,12 )

selected_sates = c(selected_sates, add_states) %>% unique()
res_state[,chart:=0]
res_state[fips %in% selected_sates, chart:=1]

# remove na
res_state<-res_state[!is.na(population_m)]

# infection rate by linear regression
i=1
inf_r = list()
for (f in unique(res_state$fips) ){
  
  dt_st<-res_state[fips==f] %>% setorder(date)
  
  # skip less than 5 days
  if(nrow(dt_st)<5) next
  
  # keep last 8 days
  dt_st <- tail(dt_st, 8)
  
  
  # liear model
  lin_mod <- lm(total_cases_per_m ~ day, data=dt_st)
  
  inf_r[[i]] = data.table(
    state = dt_st$state[1],
    fips = dt_st$fips[1],
    cases_per_m = last(dt_st$total_cases_per_m) %>% round(),
    infection_rate_per_m = lin_mod$coefficients[2] %>% round(1)
  )
  i = i + 1
}

# aggregate
infection_rate_states = rbindlist(inf_r)

# ---- save data ----
fwrite(final_res, "coronavirus_daily.csv")
fwrite(infection_rate_countries, "coronavirus_infection_rate_counries.csv")
fwrite(res_state, "coronavirus_daily_states.csv")
fwrite(infection_rate_states, "coronavirus_infection_rate_states.csv")

# ---- push to bucket ----
gcs_auth("E:/Rcode/protean-impact-183202-0e008c1e947d.json")

gcs_upload("coronavirus_daily.csv", bucket = "vk-coronavirus", predefinedAcl = "public")
gcs_upload("coronavirus_infection_rate_counries.csv", bucket = "vk-coronavirus", predefinedAcl = "public")
gcs_upload("coronavirus_daily_states.csv", bucket = "vk-coronavirus", predefinedAcl = "public")
gcs_upload("coronavirus_infection_rate_states.csv", bucket = "vk-coronavirus", predefinedAcl = "public")


