"""
This module contains common functions used throughout the application.
"""

import json  # for testing purposes
import os  # for testing purposes


def get_linkedin_id(profile) -> str:
    """
    Get the profile from the JSON file.

    Returns:
        str: LinkedIn profile id
    """

    # get "externalIds" from the profile
    external_ids = profile.get("externalIds")

    # extract exteral id for linkedin
    linkedin = next(
        (person_id for person_id in external_ids if person_id["type"] == "member-id"),
        None,
    )

    if linkedin is None:
        raise ValueError("LinkedIn ID not found in the profile")
    else:
        return linkedin["externalId"]


# function to join text from several keys
def join_keys(data, keys, separator="\n") -> str:
    """
    Joins the keys of a nested dictionary.

    Args:
        data (dict): The nested dictionary to join the keys from.
        keys (list): The keys to join.
        separator (str, optional): The separator to use between the keys. Defaults to "\n".

    Returns:
        str: The joined keys.

    """

    result = []

    # check if the data is a dictionary
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if value:
                if isinstance(value, dict):
                    keys = value.keys()
                    result.append(join_keys(value, keys, separator))
                else:
                    # only appnend is value is not a number
                    if not isinstance(value, (int, float)):
                        result.append(str(value))

    # check if the data is a list
    elif isinstance(data, list):
        for item in data:
            keys = item.keys()
            result.append(join_keys(item, keys, separator))

    # join only unique results
    result = list(set(result))

    # clean up text
    result = [
        s.replace("{", "").replace("}", "").replace("[", "").replace("]", "")
        for s in result
    ]

    return separator.join(result)


# test
if __name__ == "__main__" and os.path.exists("profile.json"):
    with open("profile.json", "r") as f:
        profile = json.load(f)

    # test get_linkedin_id function
    linkedin_id = get_linkedin_id(profile)
    print(linkedin_id)

    # summary function
    summary = join_keys(profile, ["currentPosition", "educations", "positions"])
    print(summary)
