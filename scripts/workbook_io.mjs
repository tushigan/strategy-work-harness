import fs from 'node:fs/promises';
import path from 'node:path';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';

const modules = process.env.HARNESS_NODE_MODULES;
const requireFrom = createRequire(modules ? path.resolve(modules, '../package.json') : import.meta.url);
let api;
try {
  api = await import(pathToFileURL(requireFrom.resolve('@oai/artifact-tool')).href);
} catch {
  throw new Error('缺少 Artifact Tool。请按运行环境说明配置 HARNESS_NODE_MODULES，不要把依赖复制进项目包。');
}
const { FileBlob, SpreadsheetFile, Workbook } = api;
const [mode, input, output, option] = process.argv.slice(2);

if (mode === 'help') {
  console.log(JSON.stringify(Workbook.create().help(input)));
} else if (mode === 'render') {
  const wb = await SpreadsheetFile.importXlsx(await FileBlob.load(input));
  const specs = JSON.parse(await fs.readFile(option, 'utf8'));
  await fs.mkdir(output, { recursive: true });
  for (const [index, spec] of specs.entries()) {
    const png = await wb.render({ ...spec, scale: 1.4, format: 'png' });
    await fs.writeFile(path.join(output, `sheet-${index + 1}.png`), new Uint8Array(await png.arrayBuffer()));
  }
  console.log(JSON.stringify({ rendered: specs.length }));
} else if (mode === 'write') {
  const spec = JSON.parse(await fs.readFile(input, 'utf8'));
  const wb = await SpreadsheetFile.importXlsx(await FileBlob.load(spec.template));
  const sheet = wb.worksheets.getItem(spec.sheet);
  const width = spec.headers.length;
  const n = spec.rows.length;
  const rowStyle = sheet.getRangeByIndexes(1, 0, 1, width);
  if (n > 1) {
    for (let i = 2; i <= n; i++) sheet.getRangeByIndexes(i, 0, 1, width).copyFrom(rowStyle, 'all');
  }
  sheet.getRangeByIndexes(1, 0, Math.max(1, n, spec.oldDataRows), width).clear({ applyTo: 'contents' });
  if (n) {
    const dateCols = spec.dateFields.map(field => spec.headers.indexOf(field));
    const values = spec.rows.map(row => row.map((value, col) => {
      if (value && dateCols.includes(col)) return new Date(`${value}T00:00:00Z`);
      return typeof value === 'string' && value.startsWith('=') ? `'${value}` : value;
    }));
    sheet.getRangeByIndexes(1, 0, n, width).values = values;
    sheet.getRangeByIndexes(1, 0, n, width).format.wrapText = true;
    sheet.getRangeByIndexes(1, 0, n, width).format.verticalAlignment = 'top';
    sheet.getRangeByIndexes(1, 0, n, width).format.rowHeight = spec.rowHeight || 96;
    for (const field of spec.dateFields) {
      const col = spec.headers.indexOf(field);
      if (col >= 0) sheet.getRangeByIndexes(1, col, n, 1).setNumberFormat('yyyy-mm-dd');
    }
  }
  wb.recalculate();
  const inspected = await wb.inspect({ kind: 'table', range: `'${spec.sheet}'!A1:AI${Math.min(n + 1, 8)}`,
    include: 'values,formulas', tableMaxRows: 8, tableMaxCols: 35 });
  const errors = await wb.inspect({ kind: 'match', searchTerm: '#REF!|#DIV/0!|#VALUE!|#NAME\\?|#NUM!',
    options: { useRegex: true, maxResults: 30 } });
  const xlsx = await SpreadsheetFile.exportXlsx(wb);
  await xlsx.save(output);
  console.log(JSON.stringify({ rows: n, columns: width, inspection: inspected.ndjson, errorScan: errors.ndjson }));
} else if (mode === 'patch-fixture') {
  const spec = JSON.parse(await fs.readFile(input, 'utf8'));
  const wb = await SpreadsheetFile.importXlsx(await FileBlob.load(spec.template));
  for (const edit of spec.edits) {
    const range = wb.worksheets.getItem(edit.sheet).getRange(edit.range);
    range.values = edit.values;
  }
  wb.recalculate();
  await (await SpreadsheetFile.exportXlsx(wb)).save(output);
} else if (mode === 'fixture') {
  const spec = JSON.parse(await fs.readFile(input, 'utf8'));
  const wb = Workbook.create();
  for (const tab of spec.sheets) {
    const sheet = wb.worksheets.add(tab.name);
    sheet.getRangeByIndexes(0, 0, tab.rows.length, tab.rows[0].length).values = tab.rows;
    sheet.getUsedRange().format.font = { name: 'Arial', size: 11 };
    sheet.getUsedRange().format.wrapText = true;
    sheet.getUsedRange().format.columnWidth = 32;
    sheet.getUsedRange().format.rowHeight = 48;
    sheet.getRangeByIndexes(0, 0, 1, tab.rows[0].length).format.font.bold = true;
  }
  wb.recalculate();
  console.log((await wb.inspect({ kind: 'sheet', include: 'id,name' })).ndjson);
  const xlsx = await SpreadsheetFile.exportXlsx(wb);
  await xlsx.save(output);
  console.log(JSON.stringify({ sheets: spec.sheets.length }));
} else {
  throw new Error('用法: workbook_io.mjs render 输入.xlsx 预览目录 范围.json | write 数据.json 输出.xlsx | fixture 数据.json 输出.xlsx');
}
