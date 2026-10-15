"""Catalog entries for business systems reached over their REST APIs (data only).

Jira and Slack are specified here; the Microsoft 365 specs live in the `_saas_microsoft*` modules.
"""

from __future__ import annotations

from awa.connectors._spec import (
    BasicAuth,
    BearerAuth,
    ConnectorSpec,
    EnvVar,
    Operation,
    Pagination,
    Param,
    body_param,
    path_param,
    plain_var,
)

JIRA = ConnectorSpec(
    name="jira",
    title="Jira",
    category="ticketing",
    summary="Search, read, create and comment on Jira issues.",
    base_url="{JIRA_URL}",
    env=(
        plain_var("JIRA_URL", "Address of your Jira site.", "https://your-company.atlassian.net"),
    ),
    auth=(
        BasicAuth(
            plain_var("JIRA_EMAIL", "Email address of the Jira account."),
            EnvVar("JIRA_API_TOKEN", "API token of that account."),
        ),
        BearerAuth(EnvVar("JIRA_TOKEN", "Personal access token (Jira Data Center).")),
    ),
    errors=("errorMessages", "errors", "message"),
    operations=(
        Operation(
            "search_issues",
            "Find issues with a JQL query (e.g. project = OPS AND status = 'In Progress').",
            path="/rest/api/3/search/jql",
            params=(
                Param("jql", "The JQL query.", required=True),
                Param(
                    "fields",
                    "Comma-separated fields to return.",
                    default="summary,status,assignee,priority,updated",
                ),
            ),
            items="issues",
            pages=Pagination(
                "cursor", next="nextPageToken", param="nextPageToken", size_param="maxResults"
            ),
        ),
        Operation(
            "get_issue",
            "Read one issue with its description and comments.",
            path="/rest/api/2/issue/{issue_key}",
            params=(
                path_param(
                    "issue_key", "Issue key, e.g. OPS-42.", pattern=r"^[A-Za-z][A-Za-z0-9_]*-\d+$"
                ),
                Param(
                    "fields",
                    "Comma-separated fields to return.",
                    default="summary,status,assignee,reporter,priority,description,comment,"
                    "labels,created,updated",
                ),
            ),
        ),
        Operation(
            "create_issue",
            "Create an issue.",
            method="POST",
            path="/rest/api/2/issue",
            params=(
                body_param(
                    "project_key",
                    "Key of the project, e.g. OPS.",
                    "fields.project.key",
                    required=True,
                ),
                body_param("summary", "One-line summary.", "fields.summary", required=True),
                body_param(
                    "issue_type", "Issue type name.", "fields.issuetype.name", default="Task"
                ),
                body_param("description", "Plain-text description.", "fields.description"),
                body_param("labels", "Labels.", "fields.labels", type="array", items="string"),
            ),
            write=True,
        ),
        Operation(
            "add_comment",
            "Add a comment to an issue.",
            method="POST",
            path="/rest/api/2/issue/{issue_key}/comment",
            params=(
                path_param(
                    "issue_key", "Issue key, e.g. OPS-42.", pattern=r"^[A-Za-z][A-Za-z0-9_]*-\d+$"
                ),
                body_param("body", "The comment text.", required=True),
            ),
            write=True,
        ),
    ),
    health=Operation("health", "Read the signed-in account.", path="/rest/api/2/myself"),
    docs="https://developer.atlassian.com/cloud/jira/platform/rest/v3/",
    setup="Create an API token at https://id.atlassian.com/manage-profile/security/api-tokens.",
)


_SLACK_PAGES = Pagination(
    "cursor", next="response_metadata.next_cursor", param="cursor", size_param="limit", size_max=200
)
_SLACK_CHANNEL = Param(
    "channel", "Channel id, e.g. C0123456789.", required=True, pattern=r"^[A-Z0-9]+$"
)

SLACK = ConnectorSpec(
    name="slack",
    title="Slack",
    category="communication",
    summary="Read Slack channels and threads, and post messages.",
    base_url="https://slack.com/api",
    auth=(BearerAuth(EnvVar("SLACK_BOT_TOKEN", "Bot token of a Slack app (xoxb-...).")),),
    ok="ok",
    errors=("error",),
    operations=(
        Operation(
            "list_channels",
            "List the channels (id, name, topic).",
            path="/conversations.list",
            params=(Param("types", "Channel types.", default="public_channel"),),
            query={"exclude_archived": "true"},
            items="channels",
            pages=_SLACK_PAGES,
        ),
        Operation(
            "read_channel",
            "Read the newest messages of a channel.",
            path="/conversations.history",
            params=(_SLACK_CHANNEL, Param("oldest", "Only messages after this timestamp.")),
            items="messages",
            pages=_SLACK_PAGES,
        ),
        Operation(
            "read_thread",
            "Read a thread (a message and its replies).",
            path="/conversations.replies",
            params=(
                _SLACK_CHANNEL,
                Param("ts", "Timestamp of the thread's first message.", required=True),
            ),
            items="messages",
            pages=_SLACK_PAGES,
        ),
        Operation(
            "post_message",
            "Post a message to a channel or thread.",
            method="POST",
            path="/chat.postMessage",
            params=(
                body_param("channel", "Channel id.", required=True, pattern=r"^[A-Z0-9]+$"),
                body_param("text", "The message (Slack markdown).", required=True),
                body_param("thread_ts", "Reply in this thread."),
            ),
            write=True,
        ),
    ),
    health=Operation("health", "Check the token.", path="/auth.test"),
    docs="https://api.slack.com/methods",
    setup=(
        "Create a Slack app, add the bot scopes channels:read, channels:history and "
        "chat:write, install it to the workspace and invite it to the channels."
    ),
)

SAAS = (SLACK, JIRA)
"""The business-system connectors besides Microsoft 365, in catalog order."""
