# SPDX-License-Identifier: Apache-2.0
# Copyright 2023 The HuggingFace Authors.

import json
import logging
import os
import random
import re
from hashlib import sha1
from http import HTTPStatus
from pathlib import Path
from typing import List, Optional, Tuple

import duckdb
import pyarrow as pa
from datasets import Audio, Features, Image, Value
from huggingface_hub import hf_hub_download
from libapi.authentication import auth_check
from libapi.exceptions import (
    ApiError,
    InvalidParameterError,
    MissingRequiredParameterError,
    UnexpectedApiError,
)
from libapi.utils import (
    Endpoint,
    are_valid_parameters,
    clean_cached_assets,
    get_json_api_error_response,
    get_json_error_response,
    get_json_ok_response,
    to_rows_list,
    try_backfill_dataset_then_raise,
)
from libcommon.processing_graph import ProcessingGraph, ProcessingStep
from libcommon.prometheus import StepProfiler
from libcommon.simple_cache import (
    CACHED_RESPONSE_NOT_FOUND,
    CacheEntry,
    get_best_response,
)
from libcommon.storage import StrPath, init_dir
from libcommon.utils import PaginatedResponse
from libcommon.viewer_utils.features import (
    get_supported_unsupported_columns,
    to_features_list,
)
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)


ROW_IDX_COLUMN = "__hf_index_id"
MAX_ROWS = 100
UNSUPPORTED_FEATURES = [Value("binary"), Audio(), Image()]

FTS_COMMAND_COUNT = (
    "SELECT COUNT(*) FROM (SELECT __hf_index_id, fts_main_data.match_bm25(__hf_index_id, ?) AS score FROM"
    " data) A WHERE score IS NOT NULL;"
)

FTS_COMMAND = (
    "SELECT * EXCLUDE (score) FROM (SELECT *, fts_main_data.match_bm25(__hf_index_id, ?) AS score FROM "
    "data) A WHERE score IS NOT NULL ORDER BY __hf_index_id OFFSET {offset} LIMIT {length};"
)
REPO_TYPE = "dataset"


def get_download_folder(
    root_directory: StrPath, dataset: str, config: str, split: str, revision: Optional[str]
) -> str:
    payload = (dataset, config, split, revision)
    hash_suffix = sha1(json.dumps(payload, sort_keys=True).encode(), usedforsecurity=False).hexdigest()[:8]
    subdirectory = "".join([c if re.match(r"[\w-]", c) else "-" for c in f"{dataset}-{hash_suffix}"])
    return f"{root_directory}/downloads/{subdirectory}"


def download_index_file(
    index_folder: str,
    target_revision: str,
    dataset: str,
    repo_file_location: str,
    hf_token: Optional[str] = None,
) -> None:
    logging.info(f"init_dir {index_folder}")
    init_dir(index_folder)

    # see https://pypi.org/project/hf-transfer/ for more details about how to enable hf_transfer
    os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "1"
    hf_hub_download(
        repo_type=REPO_TYPE,
        revision=target_revision,
        repo_id=dataset,
        filename=repo_file_location,
        local_dir=index_folder,
        local_dir_use_symlinks=False,
        token=hf_token,
    )


def full_text_search(index_file_location: str, query: str, offset: int, length: int) -> Tuple[int, pa.Table]:
    con = duckdb.connect(index_file_location, read_only=True)
    count_result = con.execute(query=FTS_COMMAND_COUNT, parameters=[query]).fetchall()
    num_total_rows = count_result[0][0]  # it will always return a non empty list with one element in a tuple
    logging.debug(f"got {num_total_rows=} results for {query=}")
    query_result = con.execute(
        query=FTS_COMMAND.format(offset=offset, length=length),
        parameters=[query],
    )
    pa_table = query_result.arrow()
    con.close()
    return (num_total_rows, pa_table)


def get_cache_entry_from_steps(
    processing_steps: List[ProcessingStep],
    dataset: str,
    config: Optional[str],
    split: Optional[str],
    processing_graph: ProcessingGraph,
    cache_max_days: int,
    hf_endpoint: str,
    hf_token: Optional[str] = None,
    hf_timeout_seconds: Optional[float] = None,
) -> CacheEntry:
    """Gets the cache from the first successful step in the processing steps list.
    If no successful result is found, it will return the last one even if it's an error,
    Checks if job is still in progress by each processing step in case of no entry found.
    Raises:
        - [`~utils.ResponseNotFoundError`]
          if no result is found.
        - [`~utils.ResponseNotReadyError`]
          if the response is not ready yet.
    Returns: the cached record
    """
    kinds = [processing_step.cache_kind for processing_step in processing_steps]
    best_response = get_best_response(kinds=kinds, dataset=dataset, config=config, split=split)
    if "error_code" in best_response.response and best_response.response["error_code"] == CACHED_RESPONSE_NOT_FOUND:
        try_backfill_dataset_then_raise(
            processing_steps=processing_steps,
            processing_graph=processing_graph,
            dataset=dataset,
            hf_endpoint=hf_endpoint,
            hf_timeout_seconds=hf_timeout_seconds,
            hf_token=hf_token,
            cache_max_days=cache_max_days,
        )
    return best_response.response


def create_response(
    pa_table: pa.Table,
    dataset: str,
    config: str,
    split: str,
    cached_assets_base_url: str,
    cached_assets_directory: StrPath,
    offset: int,
    num_total_rows: int,
) -> PaginatedResponse:
    features = Features.from_arrow_schema(pa_table.schema)

    features_without_key = features.copy()
    features_without_key.pop(ROW_IDX_COLUMN, None)

    _, unsupported_columns = get_supported_unsupported_columns(
        features,
        unsupported_features=UNSUPPORTED_FEATURES,
    )
    pa_table = pa_table.drop(unsupported_columns)
    return PaginatedResponse(
        features=to_features_list(features_without_key),
        rows=to_rows_list(
            pa_table,
            dataset,
            config,
            split,
            cached_assets_base_url,
            cached_assets_directory,
            offset=offset,
            features=features,
            unsupported_columns=unsupported_columns,
            row_idx_column=ROW_IDX_COLUMN,
        ),
        num_total_rows=num_total_rows,
    )


def create_search_endpoint(
    processing_graph: ProcessingGraph,
    duckdb_index_file_directory: StrPath,
    cached_assets_base_url: str,
    cached_assets_directory: StrPath,
    target_revision: str,
    cache_max_days: int,
    hf_endpoint: str,
    external_auth_url: Optional[str] = None,
    hf_token: Optional[str] = None,
    hf_jwt_public_key: Optional[str] = None,
    hf_jwt_algorithm: Optional[str] = None,
    hf_timeout_seconds: Optional[float] = None,
    max_age_long: int = 0,
    max_age_short: int = 0,
    clean_cache_proba: float = 0.0,
    keep_first_rows_number: int = -1,
    keep_most_recent_rows_number: int = -1,
    max_cleaned_rows_number: int = -1,
) -> Endpoint:
    async def search_endpoint(request: Request) -> Response:
        revision: Optional[str] = None
        with StepProfiler(method="search_endpoint", step="all"):
            try:
                with StepProfiler(method="search_endpoint", step="validate parameters"):
                    dataset = request.query_params.get("dataset")
                    config = request.query_params.get("config")
                    split = request.query_params.get("split")
                    query = request.query_params.get("query")

                    if (
                        not dataset
                        or not config
                        or not split
                        or not query
                        or not are_valid_parameters([dataset, config, split, query])
                    ):
                        raise MissingRequiredParameterError(
                            "Parameter 'dataset', 'config', 'split' and 'query' are required"
                        )

                    offset = int(request.query_params.get("offset", 0))
                    if offset < 0:
                        raise InvalidParameterError(message="Offset must be positive")

                    length = int(request.query_params.get("length", MAX_ROWS))
                    if length < 0:
                        raise InvalidParameterError("Length must be positive")
                    if length > MAX_ROWS:
                        raise InvalidParameterError(f"Length must be less than or equal to {MAX_ROWS}")

                with StepProfiler(method="search_endpoint", step="check authentication"):
                    # if auth_check fails, it will raise an exception that will be caught below
                    auth_check(
                        dataset=dataset,
                        external_auth_url=external_auth_url,
                        request=request,
                        hf_jwt_public_key=hf_jwt_public_key,
                        hf_jwt_algorithm=hf_jwt_algorithm,
                        hf_timeout_seconds=hf_timeout_seconds,
                    )

                logging.info(f"/search {dataset=} {config=} {split=} {query=} {offset=} {length=}")

                with StepProfiler(method="search_endpoint", step="validate indexing was done"):
                    # no cache data is needed to download the index file
                    # but will help to validate if indexing was done
                    processing_steps = processing_graph.get_processing_step_by_job_type("split-duckdb-index")
                    result = get_cache_entry_from_steps(
                        processing_steps=[processing_steps],
                        dataset=dataset,
                        config=config,
                        split=split,
                        processing_graph=processing_graph,
                        hf_endpoint=hf_endpoint,
                        hf_token=hf_token,
                        hf_timeout_seconds=hf_timeout_seconds,
                        cache_max_days=cache_max_days,
                    )

                    content = result["content"]
                    http_status = result["http_status"]
                    error_code = result["error_code"]
                    revision = result["dataset_git_revision"]
                    if http_status != HTTPStatus.OK:
                        return get_json_error_response(
                            content=content,
                            status_code=http_status,
                            max_age=max_age_short,
                            error_code=error_code,
                            revision=revision,
                        )

                with StepProfiler(method="search_endpoint", step="download index file if missing"):
                    file_name = content["filename"]
                    index_folder = get_download_folder(duckdb_index_file_directory, dataset, config, split, revision)
                    repo_file_location = f"{config}/{split}/{file_name}"
                    index_file_location = f"{index_folder}/{repo_file_location}"
                    index_path = Path(index_file_location)
                    if not index_path.is_file():
                        with StepProfiler(method="search_endpoint", step="download index file"):
                            download_index_file(index_folder, target_revision, dataset, repo_file_location, hf_token)

                with StepProfiler(method="search_endpoint", step="perform FTS command"):
                    logging.debug(f"connect to index file {index_file_location}")
                    (num_total_rows, pa_table) = full_text_search(index_file_location, query, offset, length)
                    index_path.touch()

                with StepProfiler(method="search_endpoint", step="clean cache"):
                    # no need to do it every time
                    if random.random() < clean_cache_proba:  # nosec
                        if (
                            keep_first_rows_number < 0
                            and keep_most_recent_rows_number < 0
                            and max_cleaned_rows_number < 0
                        ):
                            logger.debug(
                                "Params keep_first_rows_number, keep_most_recent_rows_number and"
                                " max_cleaned_rows_number are not set. Skipping cached assets cleaning."
                            )
                        else:
                            clean_cached_assets(
                                dataset=dataset,
                                cached_assets_directory=cached_assets_directory,
                                keep_first_rows_number=keep_first_rows_number,
                                keep_most_recent_rows_number=keep_most_recent_rows_number,
                                max_cleaned_rows_number=max_cleaned_rows_number,
                            )

                with StepProfiler(method="search_endpoint", step="create response"):
                    response = create_response(
                        pa_table,
                        dataset,
                        config,
                        split,
                        cached_assets_base_url,
                        cached_assets_directory,
                        offset,
                        num_total_rows,
                    )
                with StepProfiler(method="search_endpoint", step="generate the OK response"):
                    return get_json_ok_response(response, max_age=max_age_long, revision=revision)
            except Exception as e:
                error = e if isinstance(e, ApiError) else UnexpectedApiError("Unexpected error.", e)
                with StepProfiler(method="search_endpoint", step="generate API error response"):
                    return get_json_api_error_response(error=error, max_age=max_age_short, revision=revision)

    return search_endpoint