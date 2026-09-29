from typing import Literal

type DatabaseSchemaContract = Literal[
    "identity-v20",
    "school-facts-f1-v21",
    "school-facts-f1-v22",
    "school-facts-f2-v23",
    "school-facts-f2-v24",
    "school-facts-f2-v25",
    "school-facts-f3-v26",
    "school-facts-f3-v27",
    "school-facts-f4-v28",
    "school-facts-f4-v29",
    "school-facts-f4-v30",
    "school-facts-f4-v31",
    "school-facts-f5-v32",
    "school-facts-f5-v33",
    "school-facts-f5-v34",
    "school-facts-f5-v35",
    "school-facts-f5-v36",
    "school-facts-f5-v37",
    "school-facts-f5-v38",
    "school-facts-f5-v39",
    "school-facts-f5-v40",
    "school-facts-f5-v41",
    "school-facts-f6-v44",
    "school-facts-f6-v45",
    "school-facts-f7-v47",
    "school-facts-f7-v48",
    "school-facts-f7-v49",
    "school-facts-f7-v50",
    "school-facts-f7-v51",
    "school-facts-f7-v52",
    "school-facts-f7-v53",
    "school-facts-f7-v54",
    "school-facts-f7-v55",
    "school-facts-f7-v56",
    "school-facts-live-v57",
]


def schema_contract_head(contract: DatabaseSchemaContract) -> str:
    if contract == "identity-v20":
        return "20260824_20"
    if contract == "school-facts-f1-v21":
        return "20260905_21"
    if contract == "school-facts-f1-v22":
        return "20260905_22"
    if contract == "school-facts-f2-v23":
        return "20260906_23"
    if contract == "school-facts-f2-v24":
        return "20260906_24"
    if contract == "school-facts-f2-v25":
        return "20260906_25"
    if contract == "school-facts-f3-v26":
        return "20260913_26"
    if contract == "school-facts-f3-v27":
        return "20260913_27"
    if contract == "school-facts-f4-v28":
        return "20260913_28"
    if contract == "school-facts-f4-v29":
        return "20260913_29"
    if contract == "school-facts-f4-v30":
        return "20260913_30"
    if contract == "school-facts-f4-v31":
        return "20260913_31"
    if contract == "school-facts-f5-v32":
        return "20260914_32"
    if contract == "school-facts-f5-v33":
        return "20260914_33"
    if contract == "school-facts-f5-v34":
        return "20260914_34"
    if contract == "school-facts-f5-v35":
        return "20260914_35"
    if contract == "school-facts-f5-v36":
        return "20260914_36"
    if contract == "school-facts-f5-v37":
        return "20260914_37"
    if contract == "school-facts-f5-v38":
        return "20260914_38"
    if contract == "school-facts-f5-v39":
        return "20260914_39"
    if contract == "school-facts-f5-v40":
        return "20260914_40"
    if contract == "school-facts-f5-v41":
        return "20260914_41"
    if contract == "school-facts-f6-v44":
        return "20260922_44"
    if contract == "school-facts-f6-v45":
        return "20260922_45"
    if contract == "school-facts-f7-v47":
        return "20260924_47"
    if contract == "school-facts-f7-v48":
        return "20260924_48"
    if contract == "school-facts-f7-v49":
        return "20260924_49"
    if contract == "school-facts-f7-v50":
        return "20260924_50"
    if contract == "school-facts-f7-v51":
        return "20260924_51"
    if contract == "school-facts-f7-v52":
        return "20260924_52"
    if contract == "school-facts-f7-v53":
        return "20260924_53"
    if contract == "school-facts-f7-v54":
        return "20260924_54"
    if contract == "school-facts-f7-v55":
        return "20260924_55"
    if contract == "school-facts-f7-v56":
        return "20260924_56"
    if contract == "school-facts-live-v57":
        return "20260928_57"
    raise ValueError("Unsupported database schema contract.")
