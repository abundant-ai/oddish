"use client";

import { useMemo } from "react";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

const MAX_RENDERED_ROWS = 5_000;

interface CsvRendererProps {
  content: string;
  delimiter?: string;
}

function parseCSV(text: string, delimiter: string): string[][] {
  const rows: string[][] = [];
  let current = "";
  let inQuotes = false;
  let row: string[] = [];

  for (let i = 0; i < text.length; i++) {
    const char = text[i];
    const next = text[i + 1];

    if (inQuotes) {
      if (char === '"' && next === '"') {
        current += '"';
        i++;
      } else if (char === '"') {
        inQuotes = false;
      } else {
        current += char;
      }
    } else {
      if (char === '"') {
        inQuotes = true;
      } else if (char === delimiter) {
        row.push(current);
        current = "";
      } else if (char === "\n" || (char === "\r" && next === "\n")) {
        row.push(current);
        current = "";
        if (row.some((cell) => cell.length > 0)) {
          rows.push(row);
        }
        row = [];
        if (char === "\r") i++;
      } else {
        current += char;
      }
    }
  }

  if (current.length > 0 || row.length > 0) {
    row.push(current);
    if (row.some((cell) => cell.length > 0)) {
      rows.push(row);
    }
  }

  return rows;
}

export function CsvRenderer({ content, delimiter = "," }: CsvRendererProps) {
  const rows = useMemo(
    () => parseCSV(content, delimiter),
    [content, delimiter],
  );

  if (rows.length === 0) {
    return <div className="p-4 text-muted-foreground">Empty file</div>;
  }

  const headers = rows[0];
  const allDataRows = rows.slice(1);
  const totalRows = allDataRows.length;
  const truncated = totalRows > MAX_RENDERED_ROWS;
  const dataRows = truncated
    ? allDataRows.slice(0, MAX_RENDERED_ROWS)
    : allDataRows;

  return (
    <div className="flex flex-col gap-2">
      <div className="max-h-[600px] overflow-auto">
        <Table className="border-collapse text-xs">
          <TableHeader className="sticky top-0 z-10">
            <TableRow className="bg-muted/80 backdrop-blur-xs">
              <TableHead className="w-10 border-b border-r border-border px-3 py-2 text-left text-xs font-medium text-muted-foreground">
                #
              </TableHead>
              {headers.map((header, i) => (
                <TableHead
                  key={i}
                  className="whitespace-nowrap border-b border-r border-border px-3 py-2 text-left text-xs font-medium text-foreground"
                >
                  {header}
                </TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {dataRows.map((row, rowIdx) => (
              <TableRow
                key={rowIdx}
                className="transition-colors hover:bg-muted/50"
              >
                <TableCell className="border-b border-r border-border px-3 py-1.5 text-xs tabular-nums text-muted-foreground">
                  {rowIdx + 1}
                </TableCell>
                {headers.map((_, colIdx) => (
                  <TableCell
                    key={colIdx}
                    className="max-w-[300px] truncate whitespace-nowrap border-b border-r border-border px-3 py-1.5 text-xs text-foreground"
                  >
                    {row[colIdx] ?? ""}
                  </TableCell>
                ))}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
      <div className="px-4 pb-3 text-xs text-muted-foreground">
        {truncated
          ? `Showing ${MAX_RENDERED_ROWS.toLocaleString()} of ${totalRows.toLocaleString()} rows × ${headers.length} columns`
          : `${totalRows.toLocaleString()} rows × ${headers.length} columns`}
      </div>
    </div>
  );
}
