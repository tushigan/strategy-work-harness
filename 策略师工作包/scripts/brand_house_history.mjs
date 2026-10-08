import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import './brand_house_validate.js';
import './brand_house_core.js';

const core = globalThis.BHCore;
export const { validate, validateSnapshot, current, append, compare, capacity, encode, clone, parse, parseJSON } = core;
export default core;

function read(input) {
  let bytes;
  try { bytes = readFileSync(!input || input === '-' ? 0 : input); }
  catch { throw new Error('无法读取输入文件或标准输入，请检查路径和读取权限'); }
  let raw;
  try { raw = new TextDecoder('utf-8', { fatal: true }).decode(bytes); }
  catch { throw new Error('输入不是有效 UTF-8 文本，未替换或截断数据'); }
  return parse(raw);
}

if (process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url) {
  try {
    const [command, input, second, ...extra] = process.argv.slice(2);
    if (extra.length || !['validate', 'capacity', 'compare'].includes(command) ||
        (command === 'compare' ? !input || !second || input === '-' || second === '-' : second !== undefined)) {
      throw new Error('用法：validate [数据.json|-] 或 capacity [数据.json|-]，省略文件时读标准输入；compare 左.json 右.json。命令只读，不保存文件');
    }
    const data = read(input);
    const result = command === 'validate' ? encode(data) : JSON.stringify(command === 'capacity' ? capacity(data) : compare(current(data).snapshot, current(read(second)).snapshot));
    process.stdout.write(`${result}\n`);
  } catch (error) {
    const message = error instanceof Error && /[\u3400-\u9fff]/u.test(error.message) ? error.message : '数据校验失败，未修改文件';
    process.stderr.write(`错误：${message}\n`); process.exitCode = 1;
  }
}
