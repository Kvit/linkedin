from fastapi import FastAPI, HTTPException, Request

# import json
import os

from functions import join_keys

# Assume the google-cloud-firestore import is available in the development environment
from google.cloud import firestore

app = FastAPI()

# Initialize Firestore client with specific project ID, only if file is found
if os.path.isfile("vk-linkedin-master-service-account.json"):
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = (
        "vk-linkedin-master-service-account.json"
    )

db = firestore.Client(project="vk-linkedin", database="linkedin")


@app.post("/add-profile/")
async def add_or_update_item(request: Request):
    try:
        item = await request.json()

        # save item as json file
        # with open("profile.json", "w") as f:
        #     json.dump(item, f)

        document_id = item["id"]

        # join several keys from the profile
        item["summary"] = join_keys(
            item, ["miniProfile", "currentPosition", "positions", "occupation", "extra"]
        )

        # Reference the specific document in the 'extracted' collection of 'db' database
        doc_ref = db.collection("extracted").document(document_id)

        # This will add or update the document with the specified ID
        doc_ref.set(item)

        return {
            "success": True,
            "document_id": document_id,
            "summary": str(item["summary"]),
        }

    except ValueError as e:
        # Handle validation errors
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        # Handle other exceptions
        raise HTTPException(status_code=500, detail=str(e))


# echo API on default path
@app.get("/echo/{text}")
async def echo(text: str):
    print(text)
    return {"You entered": text}


# Note: Adjust the GOOGLE_APPLICATION_CREDENTIALS path and ensure Firestore is properly configured in your environment.
