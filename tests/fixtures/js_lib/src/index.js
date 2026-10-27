/** Public surface of jslib. */
import fs from 'node:fs';
import path from 'path';
import { helper } from './util';

export { parse } from './parser';
const _ = require('lodash');

export function main(file) {
  return _.trim(helper(fs.readFileSync(path.resolve(file), 'utf8')));
}
