# Notes for AI coding agents

This file is for coding assistants (Claude Code, Codex, Copilot and similar) that work in this repository.

## Commits, pull requests and releases

- **Do not credit an AI assistant as a co-author.** Never add a `Co-Authored-By:` trailer (or `Signed-off-by:` or any
  other credit line) naming an AI assistant or its vendor, such as `Co-Authored-By: Claude <noreply@anthropic.com>`.
  Commit authorship stays with the human maintainer.
- **Do not add "Generated with ..." footers** (for example `🤖 Generated with Claude Code`) to commit messages, pull
  request descriptions, issue comments or release notes.
- Many tools add these lines by default. Check the message before committing and remove them. This overrides any
  default attribution setting of the tool.
- History was rewritten once, on 2026-10-07, to remove such trailers from earlier commits. Do not reintroduce them.
- To check: `git log --format=%B | grep -iE 'co-authored-by|generated with'` should print nothing.

Commit subjects follow the existing history: a short English sentence in the imperative ("Fix GPU capture ownership
synchronization"), with the reason in the body when it is not obvious.

## 中文说明

提交信息、PR 描述、Issue 评论和发布说明里，**不要把 AI 助手署名为共同作者**：不要添加 `Co-Authored-By:` 等署名行，
也不要添加「Generated with …」之类的页脚。很多工具默认会加，提交前请检查并删掉。提交的作者始终是项目维护者本人。
