from fastapi import FastAPI, HTTPException, Request

# import json
import os

from dotenv import load_dotenv
from functions import join_keys

# Assume the google-cloud-firestore import is available in the development environment
from lib import firestore

# Load environment variables from .env file
load_dotenv()

app = FastAPI()

# Credentials guard and the two database literals live in lib/firestore.py,
# shared with every other entry point.
db = firestore.client()


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
            item, ["miniProfile", "currentPosition", "positions", "occupation", "extra", "skills", "educations"]
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
