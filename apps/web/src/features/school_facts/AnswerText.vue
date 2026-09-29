<script setup lang="ts">
import { computed } from "vue";

const props = defineProps<{ text: string }>();
type TextPart = { tag: "span" | "strong" | "em"; text: string };
type TextBlock = { tag: "p" | "ul" | "ol"; lines: string[]; start?: number };
type TableBlock = { tag: "table"; header: string[]; rows: string[][] };

// Only these text constructs are supported. Every value remains a Vue text node;
// HTML, links and code are never interpreted as executable markup.
function parts(text: string): TextPart[] {
  const result: TextPart[] = [];
  const emphasis = /`[^`\n]*`|\*\*[^*\n]+\*\*|\*[^*\n]+\*/g;
  let end = 0;
  for (const match of text.matchAll(emphasis)) {
    const index = match.index!;
    if (index > end) result.push({ tag: "span", text: text.slice(end, index) });
    const value = match[0];
    result.push(value.startsWith("`") ? { tag: "span", text: value } : value.startsWith("**")
      ? { tag: "strong", text: value.slice(2, -2) } : { tag: "em", text: value.slice(1, -1) });
    end = index + value.length;
  }
  if (end < text.length) result.push({ tag: "span", text: text.slice(end) });
  return result;
}

function tableCells(line: string): string[] | undefined {
  const cells: string[] = [];
  let cell = "";
  for (let index = 0; index < line.length; index++) {
    const char = line[index];
    if (char === "\\" && line[index + 1] === "|") { cell += "|"; index++; }
    else if (char === "\\" && line[index + 1] === "\\") { cell += "\\\\"; index++; }
    else if (char === "`" && line.indexOf("`", index + 1) !== -1) {
      const end = line.indexOf("`", index + 1);
      cell += line.slice(index, end + 1); index = end;
    } else if (char === "|") { cells.push(cell.trim()); cell = ""; }
    else cell += char;
  }
  if (!cells.length) return undefined;
  cells.push(cell.trim());
  if (!cells[0]) cells.shift();
  if (!cells[cells.length - 1]) cells.pop();
  return cells;
}

/** Accept only a header, a matching separator and consistent pipe rows. */
function tableAt(lines: string[], start: number): { block: TableBlock; end: number } | undefined {
  const header = tableCells(lines[start]);
  const separator = tableCells(lines[start + 1] ?? "");
  if (!header?.length || separator?.length !== header.length || !separator.every(cell => /^:?-{3,}:?$/.test(cell))) return;
  const rows: string[][] = [];
  let end = start + 2;
  for (; end < lines.length; end++) {
    const cells = tableCells(lines[end]);
    if (!cells) break;
    if (cells.length !== header.length) return;
    rows.push(cells);
  }
  return { block: { tag: "table", header, rows }, end };
}

const blocks = computed(() => {
  const result: Array<TextBlock | TableBlock> = [];
  let current: TextBlock | undefined;
  let fence: string | undefined;
  const lines = props.text.replace(/\r\n?/g, "\n").split("\n");
  for (let index = 0; index < lines.length; index++) {
    const line = lines[index];
    if (!line.trim()) { current = undefined; continue; }
    const marker = /^ {0,3}(`{3,}|~{3,})/.exec(line)?.[1];
    if (marker) fence = fence && marker[0] === fence[0] && marker.length >= fence.length ? undefined : fence ?? marker;
    if (!fence && !marker) {
      const table = tableAt(lines, index);
      if (table) { result.push(table.block); index = table.end - 1; current = undefined; continue; }
    }
    const bullet = /^ {0,3}[-+*]\s+(.+)$/.exec(line);
    const ordered = /^ {0,3}(\d{1,9})[.)]\s+(.+)$/.exec(line);
    const tag = bullet ? "ul" : ordered ? "ol" : "p";
    const start = ordered ? Number(ordered[1]) : undefined;
    if (!current || current.tag !== tag || (tag === "ol" && start !== current.start! + current.lines.length)) {
      current = { tag, lines: [], ...(start !== undefined ? { start } : {}) };
      result.push(current);
    }
    current.lines.push(bullet?.[1] ?? ordered?.[2] ?? line);
  }
  return result;
});
</script>

<template>
  <div class="answer-text">
    <template v-for="(block, index) in blocks" :key="index">
      <div v-if="block.tag === 'table'" class="answer-table"><table><thead><tr><th v-for="(cell, cellIndex) in block.header" :key="cellIndex" scope="col"><component :is="part.tag" v-for="(part, partIndex) in parts(cell)" :key="partIndex">{{ part.text }}</component></th></tr></thead><tbody><tr v-for="(row, rowIndex) in block.rows" :key="rowIndex"><td v-for="(cell, cellIndex) in row" :key="cellIndex"><component :is="part.tag" v-for="(part, partIndex) in parts(cell)" :key="partIndex">{{ part.text }}</component></td></tr></tbody></table></div>
      <p v-else-if="block.tag === 'p'"><component :is="part.tag" v-for="(part, partIndex) in parts(block.lines.join('\n'))" :key="partIndex">{{ part.text }}</component></p>
      <component :is="block.tag" v-else :start="block.start">
        <li v-for="(line, lineIndex) in block.lines" :key="lineIndex"><component :is="part.tag" v-for="(part, partIndex) in parts(line)" :key="partIndex">{{ part.text }}</component></li>
      </component>
    </template>
  </div>
</template>

<style scoped>
.answer-text { line-height: 1.7; overflow-wrap: anywhere; }
p, ul, ol { margin: .3rem 0 .7rem; }
p, li { white-space: pre-wrap; }
ul, ol { padding-left: 1.5rem; }
li + li { margin-top: .2rem; }
.answer-table { max-width: 100%; overflow-x: auto; margin: .3rem 0 .7rem; }
table { width: 100%; border-collapse: collapse; }
th, td { padding: .5rem .65rem; text-align: left; vertical-align: top; border: 1px solid #d5e3e3; }
th { background: #f2f7f7; }
</style>
