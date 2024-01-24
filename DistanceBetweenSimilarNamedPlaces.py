import pandas as pd
from tqdm import tqdm
import haversine as hs
import csv

pop_limit = 500
input_file = fr'C:\Users\andys\OneDrive\Documents\1. HOME\Programming\DistanceBetweenPlaces\cities{pop_limit}.txt'
output_file = fr'C:\Users\andys\OneDrive\Documents\1. HOME\Programming\DistanceBetweenPlaces\output_{pop_limit}.csv'
headers = ['place1', 'lat1', 'lon1', 'place2', 'lat2', 'lon2', 'havesinedistance']

def different_by_n_letters(str1, str2, n):
    try:
        #print(str1, str2)
        if len(str1) != len(str2):
            return False

        diff_count = 0
        for i in range(len(str1)):
            if str1[i] != str2[i]:
                diff_count += 1
        #print(diff_count)
        if diff_count == n:
            #print(str1, str2)
            return True
        
        return False
    except:
        return False

def haversine_distance(lat1, lon1, lat2, lon2):
    loc1 = (lat1, lon1)
    loc2 = (lat2, lon2)
    dist = hs.haversine(loc1, loc2)
    return dist

colnames = ['geonameid', 'name', 'asciiname', 'alternatenames', 'latitude', 'longitude', 'featureclass', 'featurecode', 'countrycode', 'cc2', 'admin1code', 'admin2code', 'admin3code','admin4code', 'population', 'elevation', 'dem', 'timezone', 'modificationdate']
df = pd.read_table(input_file, names=colnames)


place_names = df['name'].tolist()
ascii_names = df['asciiname'].tolist()
latitudes = df['latitude'].tolist()
longitudes = df['longitude'].tolist()
place_name_count = len(place_names)

with open(output_file, 'w', newline='') as file:
    writer = csv.writer(file, delimiter=',')
    writer.writerow(headers)

with open(output_file, 'a', newline='') as file:
    writer = csv.writer(file, delimiter=',')
    for place in tqdm(range(place_name_count)):
        for place2 in range(place, place_name_count):
            if different_by_n_letters(ascii_names[place], ascii_names[place2], 1):
                distance = haversine_distance(latitudes[place], longitudes[place], latitudes[place2], longitudes[place2])
                writer.writerow([ascii_names[place],latitudes[place], longitudes[place],ascii_names[place2],latitudes[place2], longitudes[place2], distance])
            
            
