"""MIMIC-IV query API.

Supports querying of MIMICIV-2.0.

"""

# pylint: disable=duplicate-code

import logging
from typing import List, Optional

from sqlalchemy import Integer, func, select
from sqlalchemy.sql.selectable import Subquery

import cyclops.query.ops as qo
from cyclops.process.column_names import (
    ADMIT_TIMESTAMP,
    AGE,
    DATE_OF_DEATH,
    DIAGNOSIS_CODE,
    DIAGNOSIS_TITLE,
    DIAGNOSIS_VERSION,
    DISCHARGE_TIMESTAMP,
    ENCOUNTER_ID,
    EVENT_CATEGORY,
    EVENT_NAME,
    EVENT_TIMESTAMP,
    EVENT_VALUE,
    EVENT_VALUE_UNIT,
    SEX,
    SUBJECT_ID,
)
from cyclops.query.base import DatasetQuerier
from cyclops.query.interface import QueryInterface, QueryInterfaceProcessed
from cyclops.query.post_process.mimiciv import process_mimic_care_units
from cyclops.query.util import (
    TableTypes,
    assert_table_has_columns,
    ckwarg,
    get_column,
    remove_kwargs,
    table_params_to_type,
)
from cyclops.utils.log import setup_logging

# Logging.
LOGGER = logging.getLogger(__name__)
setup_logging(print_level="INFO", logger=LOGGER)


# Constants.
PATIENTS = "patients"
ADMISSIONS = "admissions"
DIAGNOSES = "diagnoses"
PATIENT_DIAGNOSES = "patient_diagnoses"
EVENT_LABELS = "event_labels"
EVENTS = "events"
TRANSFERS = "transfers"
ED_STAYS = "ed_stays"
TABLE_MAP = {
    PATIENTS: lambda db: db.mimiciv_hosp.patients,
    ADMISSIONS: lambda db: db.mimiciv_hosp.admissions,
    DIAGNOSES: lambda db: db.mimiciv_hosp.d_icd_diagnoses,
    PATIENT_DIAGNOSES: lambda db: db.mimiciv_hosp.diagnoses_icd,
    EVENT_LABELS: lambda db: db.mimiciv_icu.d_items,
    EVENTS: lambda db: db.mimiciv_icu.chartevents,
    TRANSFERS: lambda db: db.mimiciv_hosp.transfers,
    ED_STAYS: lambda db: db.mimic_ed.edstays,
}
COLUMN_MAP = {
    "hadm_id": ENCOUNTER_ID,
    "subject_id": SUBJECT_ID,
    "admittime": ADMIT_TIMESTAMP,
    "dischtime": DISCHARGE_TIMESTAMP,
    "gender": SEX,
    "anchor_age": AGE,
    "valueuom": EVENT_VALUE_UNIT,
    "label": EVENT_NAME,
    "valuenum": EVENT_VALUE,
    "charttime": EVENT_TIMESTAMP,
    "icd_code": DIAGNOSIS_CODE,
    "icd_version": DIAGNOSIS_VERSION,
    "dod": DATE_OF_DEATH,
}


class MIMICIVQuerier(DatasetQuerier):
    """MIMICIV dataset querier."""

    def __init__(self, **config_overrides):
        """Initialize.

        Parameters
        ----------
        **config_overrides
            Override configuration parameters, specified as kwargs.

        """
        overrides = {}
        if config_overrides:
            overrides = config_overrides
        super().__init__(TABLE_MAP, COLUMN_MAP, **overrides)

    def patients(self, **process_kwargs) -> QueryInterface:
        """Query MIMIC patient data.

        Other Parameters
        ----------------
        sex: str or list of string, optional
            Specify patient sex (one or multiple).
        died: bool, optional
            Specify True to get patients who have died, and False for those who haven't.
        limit: int, optional
            Limit the number of rows returned.

        """
        table = self.get_table(PATIENTS)

        # Process and include patient's anchor year.
        table = select(
            table,
            (
                func.substr(get_column(table, "anchor_year_group"), 1, 4).cast(Integer)
            ).label("anchor_year_group_start"),
            (
                func.substr(get_column(table, "anchor_year_group"), 8, 12).cast(Integer)
            ).label("anchor_year_group_end"),
        ).subquery()

        # Select the middle of the anchor year group as the anchor year
        table = select(
            table,
            (
                get_column(table, "anchor_year_group_start")
                + (
                    get_column(table, "anchor_year_group_end")
                    - get_column(table, "anchor_year_group_start")
                )
                / 2
            ).label("anchor_year_group_middle"),
        ).subquery()

        table = select(
            table,
            (
                get_column(table, "anchor_year_group_middle")
                - get_column(table, "anchor_year")
            ).label("anchor_year_difference"),
        ).subquery()

        # Shift relevant columns by anchor year difference
        table = qo.AddColumn("anchor_year", "anchor_year_difference")(table)
        table = qo.AddDeltaColumns([DATE_OF_DEATH], years="anchor_year_difference")(
            table
        )

        # Calculate approximate year of birth
        table = qo.AddColumn(
            "anchor_year", "age", negative=True, new_col_labels="birth_year"
        )(table)

        table = qo.Drop(
            [
                "age",
                "anchor_year",
                "anchor_year_group",
                "anchor_year_group_start",
                "anchor_year_group_end",
                "anchor_year_group_middle",
            ]
        )(table)

        # Reorder nicely.
        table = qo.Reorder(
            [SUBJECT_ID, SEX, "birth_year", DATE_OF_DEATH, "anchor_year_difference"]
        )(table)

        # Process optional operations
        if "died" not in process_kwargs and "died_binarize_col" in process_kwargs:
            process_kwargs["died"] = True

        operations: List[tuple] = [
            # Must convert to string since CHAR(1) type doesn't recognize equality
            (qo.ConditionIn, [SEX, qo.QAP("sex")], {"to_str": True}),
            (
                qo.ConditionEquals,
                ["discharge_location", "DIED"],
                {
                    "not_": qo.QAP("died", transform_fn=lambda x: not x),
                    "binarize_col": qo.QAP("died_binarize_col", required=False),
                },
            ),
            (qo.Limit, [qo.QAP("limit")], {}),
        ]

        table = qo.process_operations(table, operations, process_kwargs)

        return QueryInterface(self._db, table)

    def diagnoses(self, **process_kwargs) -> QueryInterface:
        """Query MIMIC diagnoses.

        Returns
        -------
        cyclops.query.interface.QueryInterface
            Constructed table, wrapped in an interface object.

        Other Parameters
        ----------------
        diagnosis_versions: int or list of int, optional
            Get codes having certain ICD versions.
        diagnosis_substring : str, optional
            Substring to match in the ICD code.
        diagnosis_codes : str or list of str, optional
            Get only the specified ICD codes.
        limit: int, optional
            Limit the number of rows returned.

        """
        table = self.get_table(DIAGNOSES)

        # Rename long_title
        table = qo.Rename({"long_title": DIAGNOSIS_TITLE})(table)

        # Trim whitespace from ICD codes.
        table = qo.Trim(DIAGNOSIS_CODE)(table)

        # Process optional operations
        operations: List[tuple] = [
            (
                qo.ConditionIn,
                [DIAGNOSIS_VERSION, qo.QAP("diagnosis_versions")],
                {"to_int": True},
            ),
            (
                qo.ConditionSubstring,
                [DIAGNOSIS_TITLE, qo.QAP("diagnosis_substring")],
                {},
            ),
            (
                qo.ConditionIn,
                [DIAGNOSIS_CODE, qo.QAP("diagnosis_codes")],
                {"to_str": True},
            ),
            (qo.Limit, [qo.QAP("limit")], {}),
        ]

        table = qo.process_operations(table, operations, process_kwargs)

        return QueryInterface(self._db, table)

    @table_params_to_type(Subquery)
    @assert_table_has_columns(patients_table=SUBJECT_ID)
    def patient_diagnoses(
        self, patients_table: Optional[TableTypes] = None, **process_kwargs
    ) -> QueryInterface:
        """Query MIMIC patient diagnoses.

        Parameters
        ----------
        patients: cyclops.query.util.TableTypes, optional
            Patient encounters query used to join.

        Returns
        -------
        cyclops.query.interface.QueryInterface
            Constructed table, wrapped in an interface object.

        Other Parameters
        ----------------
        diagnosis_versions: int or list of int, optional
            Get codes having certain ICD versions.
        diagnosis_substring: str, optional
            Substring to match in the ICD code.
        diagnosis_codes: str or list of str, optional
            Get only the specified ICD codes.

        """
        # Get patient diagnoses.
        table = self.get_table(PATIENT_DIAGNOSES)

        # Trim whitespace from ICD codes.
        table = qo.Trim(DIAGNOSIS_CODE)(table)

        # If provided, join with a patients table
        if patients_table is not None:
            table = qo.Join(patients_table, on=SUBJECT_ID)(table)

        # Get diagnosis codes.
        diagnoses_table = self.diagnoses(
            diagnosis_versions=ckwarg(process_kwargs, "diagnosis_versions"),
            diagnosis_substring=ckwarg(process_kwargs, "diagnosis_substring"),
            diagnosis_codes=ckwarg(process_kwargs, "diagnosis_codes"),
        ).query
        process_kwargs = remove_kwargs(
            process_kwargs,
            ["diagnosis_versions", "diagnosis_substring", "diagnosis_codes"],
        )

        # Include DIAGNOSIS_TITLE in patient diagnoses.
        table = qo.Join(
            diagnoses_table,
            on=[DIAGNOSIS_CODE, DIAGNOSIS_VERSION],
            join_table_cols=DIAGNOSIS_TITLE,
        )(table)

        return QueryInterface(self._db, table)

    @table_params_to_type(Subquery)
    @assert_table_has_columns(patients_table=SUBJECT_ID)
    def transfers(
        self, patients_table: Optional[TableTypes] = None, **process_kwargs
    ) -> QueryInterface:
        """Get care unit table within a given set of encounters.

        Parameters
        ----------
        patients_table: cyclops.query.util.TableTypes, optional
            Patient encounters used to join.

        Returns
        -------
        cyclops.query.interface.QueryInterface
            Constructed table, wrapped in an interface object.

        Other Parameters
        ----------------
        encounters : list, optional
            The encounter IDs on which to filter. If None, consider all encounters.
        limit: int, optional
            Limit the number of rows returned.

        """
        table = self.get_table(TRANSFERS)

        if patients_table is not None:
            table = qo.Join(patients_table, on=SUBJECT_ID)(table)

            table = qo.AddDeltaColumns(
                ["intime", "outtime"], years="anchor_year_difference"
            )(table)

        # Process optional operations
        operations: List[tuple] = [
            (qo.ConditionIn, [ENCOUNTER_ID, qo.QAP("encounters")], {"to_int": True}),
            (qo.Limit, [qo.QAP("limit")], {}),
        ]

        table = qo.process_operations(table, operations, process_kwargs)

        return QueryInterface(self._db, table)

    @table_params_to_type(Subquery)
    @assert_table_has_columns(patients_table=SUBJECT_ID)
    def care_units(
        self, patients_table: Optional[TableTypes] = None, **process_kwargs
    ) -> QueryInterfaceProcessed:
        """Get care unit table within a given set of encounters.

        Parameters
        ----------
        patients_table: cyclops.query.util.TableTypes, optional
            Patient encounters to join with transfers.

        Returns
        -------
        cyclops.query.interface.QueryInterfaceProcessed
            Constructed table, wrapped in an interface object.

        Other Parameters
        ----------------
        encounters : int or list of int, optional
            Get the specific encounter IDs.

        """
        table = self.transfers(
            patients_table=patients_table,
            encounters=ckwarg(process_kwargs, "encounters"),
        ).query

        process_kwargs = remove_kwargs(process_kwargs, "encounters")

        return QueryInterfaceProcessed(
            self._db,
            table,
            process_fn=lambda x: process_mimic_care_units(x, specific=False),
        )

    @table_params_to_type(Subquery)
    @assert_table_has_columns(patients_table=SUBJECT_ID)
    def patient_encounters(
        self, patients_table: Optional[TableTypes] = None, **process_kwargs
    ) -> QueryInterface:
        """Query MIMIC patient encounters.

        Parameters
        ----------
        patients_table: cyclops.query.util.TableTypes, optional
            Optionally provide a patient table when getting patient encounters.

        Returns
        -------
        cyclops.query.interface.QueryInterface
            Constructed table, wrapped in an interface object.

        Other Parameters
        ----------------
        sex: str or list of string, optional
            Specify patient sex (one or multiple).
        died: bool, optional
            Specify True to get patients who have died, and False for those who haven't.
        died_binarize_col: str, optional
            Binarize the died condition and save as a column with label
            died_binarize_col.
        before_date: datetime.datetime or str
            Get patients encounters before some date.
            If a string, provide in YYYY-MM-DD format.
        after_date: datetime.datetime or str
            Get patients encounters after some date.
            If a string, provide in YYYY-MM-DD format.
        years: int or list of int, optional
            Get patient encounters by year.
        months: int or list of int, optional
            Get patient encounters by month.
        limit: int, optional
            Limit the number of rows returned.

        """
        table = self.get_table(ADMISSIONS)

        if patients_table is None:
            patients_table = self.patients().query

        # Join admissions and patient table
        table = qo.Join(patients_table, on="subject_id")(table)

        # Update timestamps with anchor year difference
        table = qo.AddDeltaColumns(
            [
                ADMIT_TIMESTAMP,
                DISCHARGE_TIMESTAMP,
                "deathtime",
                "edregtime",
                "edouttime",
            ],
            years="anchor_year_difference",
        )(table)

        # Extract approximate age at time of admission
        table = qo.ExtractTimestampComponent(ADMIT_TIMESTAMP, "year", AGE)(table)
        table = qo.AddColumn(AGE, "birth_year", negative=True)(table)
        table = qo.ReorderAfter(AGE, SEX)(table)

        # Process optional operations
        if "died" not in process_kwargs and "died_binarize_col" in process_kwargs:
            process_kwargs["died"] = True

        operations: List[tuple] = [
            (qo.ConditionBeforeDate, [ADMIT_TIMESTAMP, qo.QAP("before_date")], {}),
            (qo.ConditionAfterDate, [ADMIT_TIMESTAMP, qo.QAP("after_date")], {}),
            (qo.ConditionInYears, [ADMIT_TIMESTAMP, qo.QAP("years")], {}),
            (qo.ConditionInMonths, [ADMIT_TIMESTAMP, qo.QAP("months")], {}),
            (qo.ConditionIn, [SEX, qo.QAP("sex")], {"to_str": True}),
            (
                qo.ConditionEquals,
                ["discharge_location", "DIED"],
                {
                    "not_": qo.QAP("died", transform_fn=lambda x: not x),
                    "binarize_col": qo.QAP("died_binarize_col", required=False),
                },
            ),
            (qo.Limit, [qo.QAP("limit")], {}),
        ]

        table = qo.process_operations(table, operations, process_kwargs)

        return QueryInterface(self._db, table)

    @table_params_to_type(Subquery)
    @assert_table_has_columns(patient_encounters_table=[ENCOUNTER_ID, SUBJECT_ID])
    def events(
        self, patient_encounters_table: Optional[TableTypes] = None, **process_kwargs
    ) -> QueryInterface:
        """Query MIMIC events.

        Parameters
        ----------
        patient_encounters_table: cyclops.query.util.TableTypes, optional
            Optionally provide a patient encounter table to join with events.

        Returns
        -------
        cyclops.query.interface.QueryInterface
            Constructed table, wrapped in an interface object.

        Other Parameters
        ----------------
        categories: str or list of str, optional
            Restrict to certain categories.
        event_names: str or list of str, optional
            Restrict to certain event names.
        event_name_substring: str, optional
            Substring to search event names to filter.
        limit: int, optional
            Limit the number of rows returned.

        """
        table = self.get_table(EVENTS)
        event_labels = self.get_table(EVENT_LABELS)

        # Get category and event name
        table = qo.Join(
            event_labels, on="itemid", join_table_cols=["category", "event_name"]
        )(table)
        table = qo.Rename({"category": EVENT_CATEGORY})(table)

        # Process optional operations
        operations: List[tuple] = [
            (qo.ConditionBeforeDate, [EVENT_TIMESTAMP, qo.QAP("before_date")], {}),
            (qo.ConditionAfterDate, [EVENT_TIMESTAMP, qo.QAP("after_date")], {}),
            (qo.ConditionInYears, [EVENT_TIMESTAMP, qo.QAP("years")], {}),
            (qo.ConditionInMonths, [EVENT_TIMESTAMP, qo.QAP("months")], {}),
            (qo.ConditionIn, [EVENT_CATEGORY, qo.QAP("categories")], {}),
            (qo.ConditionIn, [EVENT_NAME, qo.QAP("event_names")], {}),
            (qo.ConditionSubstring, [EVENT_NAME, qo.QAP("event_name_substring")], {}),
            (qo.Limit, [qo.QAP("limit")], {}),
        ]

        table = qo.process_operations(table, operations, process_kwargs)

        # Join on patient encounters
        if patient_encounters_table is not None:
            table = qo.Join(
                patient_encounters_table,
                on=ENCOUNTER_ID,
            )(table)

            # Add MIMIC patient-specific time difference to event/store timestamps
            table = qo.AddDeltaColumns(
                [EVENT_TIMESTAMP, "storetime"], years="anchor_year_difference"
            )(table)

        return QueryInterface(self._db, table)
