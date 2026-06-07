FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /root

# Plain-file inputs (the invoice workbook stays a file; only the Slack
# corrections channel moves behind the MCP).
COPY invoice_register.xlsx /root/invoice_register.xlsx
COPY workspace_readme.md /root/README.md

# Mock Slack MCP (stdio) + the channel seed. The seed lives under /opt (not in
# the agent workspace) and is served ONLY through the MCP tools. See the
# experiment README for the production hardening (root-owned seed daemon +
# non-root agent user) that fully seals it.
RUN install -d -m 0755 /opt/slack-mcp/seed
COPY mock_slack_mcp.py /usr/local/bin/mock-slack-mcp
COPY seed/billing_corrections_channel.json /opt/slack-mcp/seed/billing_corrections_channel.json
RUN chmod 0755 /usr/local/bin/mock-slack-mcp

# Skills (uplift) registered into the common agent skill directories.
COPY skills /root/.claude/skills
COPY skills /root/.codex/skills
COPY skills /root/.cursor/skills
COPY skills /root/.agents/skills
COPY skills /root/.gemini/skills

RUN pip3 install --break-system-packages \
    openpyxl==3.1.5 \
    pandas==2.2.3
