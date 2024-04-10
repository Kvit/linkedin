# downoads the data from the firestore database and saves it to a csv file

import os
import pandas as pd
from google.cloud import firestore

# Initialize Firestore client with specific project ID, only if file is found
if os.path.isfile("vk-linkedin-master-service-account.json"):
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = (
        "vk-linkedin-master-service-account.json"
    )

db = firestore.Client(project="vk-linkedin", database="linkedin")


def collection_to_csv(col, file):
    # get collection reference
    collection_ref = db.collection(col)

    # collection to pandas dataframe
    docs = collection_ref.stream()
    data = [doc.to_dict() for doc in docs]
    df = pd.DataFrame(data)

    # count the number of rows
    print(f"Number of rows: {len(df)}")

    # rename column profileUrl to profile_url
    df = df.rename(columns={"profileUrl": "profile_url"})

    # save to csv
    df.to_csv(file + ".csv", index=False)

    # save profile urls to txt
    df["profile_url"].to_csv(file + ".txt", sep="\t", index=False, header=False)


# test function
if __name__ == "__main__":
    collection_to_csv("analysis", "analysis")
