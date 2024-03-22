from fastapi import FastAPI, HTTPException, Request

# Assume the google-cloud-firestore import is available in the development environment
from google.cloud import firestore

app = FastAPI()

# Initialize Firestore client with specific project ID
project_id = "vk-project"
# os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "path/to/your/google-credentials.json"
# db = firestore.Client(project=project_id)


@app.post("/add-profile/")
async def add_or_update_item(request: Request):
    try:
        item_json = await request.json()

        # Reference the specific document in the 'extracted' collection of 'db' database
        # doc_ref = db.collection(u'db').document(u'extracted').collection(u'extracted').document(document_id)
        # doc_ref.set(item_data)  # This will add or update the document with the specified ID

        return {"success": True, "document_id": "document_id", "item": str(item_json)}
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
