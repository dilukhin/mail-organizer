"""Собственный Gmail reader с подтверждением ожидаемого OAuth-аккаунта."""
from __future__ import annotations

import re
from urllib.parse import urlencode

from .gmail import GmailHistoryAdapter, HISTORY_SCOPE_KEY, ReadFailure, sync_history
from .gmail_http import HttpFailure, exchange
from .gmail_oauth import AccessSession, OAuthFailure, mailbox
from .incremental import OfflineState, Scope

LABEL_FIELDS = "labels(id,name)"
MESSAGE_FIELDS = "id,internalDate,labelIds,payload(headers(name,value))"
HISTORY_FIELDS = "history(id,messages(id),messagesAdded(message(id)),messagesDeleted(message(id)),labelsAdded(message(id),labelIds),labelsRemoved(message(id),labelIds)),nextPageToken,historyId"


def _request(operation, parameters):
    p = dict(parameters)
    if p.pop("userId", None) != "me":
        raise ReadFailure(0)
    if operation == "labels.list" and p == {"fields": LABEL_FIELDS}:
        resource = "labels"
    elif operation == "messages.get":
        ident = p.pop("id", None)
        if not isinstance(ident, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,256}", ident):
            raise ReadFailure(0)
        if p != {"format": "metadata", "metadataHeaders": ("From", "Subject"), "fields": MESSAGE_FIELDS}:
            raise ReadFailure(0)
        resource = "messages/" + ident
    elif operation == "history.list":
        if set(p) not in ({"startHistoryId", "maxResults", "fields"}, {"startHistoryId", "maxResults", "fields", "pageToken"}):
            raise ReadFailure(0)
        if p["fields"] != HISTORY_FIELDS or type(p["maxResults"]) is not int or not 1 <= p["maxResults"] <= 100:
            raise ReadFailure(0)
        if not isinstance(p["startHistoryId"], str) or not re.fullmatch(r"[1-9][0-9]{0,31}", p["startHistoryId"]):
            raise ReadFailure(0)
        if "pageToken" in p and (not isinstance(p["pageToken"], str) or not 0 < len(p["pageToken"]) <= 2048 or any(ord(c) < 32 for c in p["pageToken"])):
            raise ReadFailure(0)
        resource = "history"
    else:
        raise ReadFailure(0)
    return "/gmail/v1/users/me/" + resource + "?" + urlencode(p, doseq=True)


class GmailHttpsReader:
    """После проверки profile использует один access token до истечения его срока."""

    def __init__(self, session: AccessSession, scope: Scope):
        if scope.provider != "gmail" or scope.scope_key != HISTORY_SCOPE_KEY:
            raise ReadFailure(0)
        self._session, self.scope = session, scope
        try:
            raw = exchange("gmail", "/gmail/v1/users/me/profile?fields=emailAddress", bearer=session.bearer())
            if set(raw) != {"emailAddress"} or mailbox(raw["emailAddress"]) != session._expected_email:
                raise ReadFailure(0)
        except HttpFailure as exc:
            raise ReadFailure(exc.status) from None
        except (OAuthFailure, ValueError, TypeError):
            raise ReadFailure(0) from None
        self.identity_sha256 = session.identity_sha256()

    def read(self, operation, parameters):
        path = _request(operation, parameters)
        try:
            return exchange("gmail", path, bearer=self._session.bearer())
        except HttpFailure as exc:
            raise ReadFailure(exc.status) from None
        except OAuthFailure:
            raise ReadFailure(401) from None


def sync_verified_history(state: OfflineState, reader: GmailHttpsReader, rules) -> str:
    state.bind_identity(reader.scope, reader.identity_sha256)
    return sync_history(state, reader.scope, GmailHistoryAdapter(reader), rules, identity_sha256=reader.identity_sha256)
