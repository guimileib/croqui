// Turns text into tokens.
import { helper } from './util/index.js';

export class Parser {}

export function parse(text) {
  return helper(text).split(' ');
}
