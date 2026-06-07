FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /root

# Plain-file inputs (ledger, bank statement, vendor master, change tickets stay
# files; only the finance-ops chat channel moves behind the MCP).
COPY invoice_ledger.csv /root/invoice_ledger.csv
COPY bank_statement.csv /root/bank_statement.csv
COPY vendor_master.csv /root/vendor_master.csv
COPY bank_change_tickets.csv /root/bank_change_tickets.csv
COPY workspace_readme.md /root/README.md

# Mock Slack MCP (stdio) + the channel seed, served ONLY through the MCP tools.
RUN install -d -m 0755 /opt/slack-mcp/seed
COPY mock_slack_mcp.py /usr/local/bin/mock-slack-mcp
COPY seed/finance_ops_channel.json /opt/slack-mcp/seed/finance_ops_channel.json
RUN chmod 0755 /usr/local/bin/mock-slack-mcp

# Skills (uplift) registered into the common agent skill directories.
COPY skills /root/.claude/skills
COPY skills /root/.codex/skills
COPY skills /root/.cursor/skills
COPY skills /root/.agents/skills
COPY skills /root/.gemini/skills
