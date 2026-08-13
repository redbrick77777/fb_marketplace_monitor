"""Google Sheets writer using a service account (no browser login needed).

One-time setup:
1. In Google Cloud Console, create/select a project and enable the
   "Google Sheets API".
2. Create a Service Account, then create a JSON key for it and download it.
3. Open your target spreadsheet and "Share" it with the service account's
   email address (looks like xxx@yyy.iam.gserviceaccount.com - it's in the
   downloaded JSON as "client_email"). Give it Editor access.
4. Point `google_service_account_file` in config.yaml at the downloaded JSON.
"""
from __future__ import annotations

import logging

import gspread
from gspread.exceptions import WorksheetNotFound

logger = logging.getLogger(__name__)

HEADER_ROW = ["Listing ID", "Title", "Price", "Distance (mi)", "Link", "Date Found", "Watch"]


class SheetsClient:
    def __init__(self, service_account_file: str):
        self._client = gspread.service_account(filename=service_account_file)

    def _get_or_create_worksheet(self, sheet_id: str, tab_name: str):
        spreadsheet = self._client.open_by_key(sheet_id)
        try:
            ws = spreadsheet.worksheet(tab_name)
        except WorksheetNotFound:
            logger.info("Tab '%s' doesn't exist yet - creating it.", tab_name)
            ws = spreadsheet.add_worksheet(title=tab_name, rows=1000, cols=len(HEADER_ROW))
            ws.append_row(HEADER_ROW)
        else:
            first_row = ws.row_values(1)
            if first_row and first_row != HEADER_ROW:
                logger.warning(
                    "Tab '%s' row 1 doesn't match the expected header %s - "
                    "leaving existing content alone and just appending rows.",
                    tab_name,
                    HEADER_ROW,
                )
            elif not first_row:
                ws.append_row(HEADER_ROW)
        return ws

    def append_rows(self, sheet_id: str, tab_name: str, rows: list[list]) -> None:
        if not rows:
            return
        ws = self._get_or_create_worksheet(sheet_id, tab_name)
        ws.append_rows(rows, value_input_option="USER_ENTERED")
