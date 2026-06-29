/**
 * Split a SKILL.md content string into the markdown body (everything after
 * the closing `---` of the frontmatter). We match `---` only on its own line
 * (the YAML frontmatter fence), so `---` appearing inside a frontmatter value
 * (e.g. a name like `my---skill`) or as a horizontal rule in the body does not
 * confuse the split. Returns the original content if no closed frontmatter.
 */
export function extractSkillMdBody(content: string): string {
  // Match a line that is exactly `---` (the opening and closing fences).
  const fence = /(^|\n)---[ \t]*(\n|$)/g;
  let count = 0;
  let match: RegExpExecArray | null;
  while ((match = fence.exec(content)) !== null) {
    count += 1;
    if (count === 2) {
      // Body is everything after the closing fence. buildSkillMd inserts a
      // blank line after it, so strip a single leading newline only —
      // preserving any intentional leading whitespace in the body itself.
      return content.slice(match.index + match[0].length).replace(/^\n/, "");
    }
  }
  return content;
}
