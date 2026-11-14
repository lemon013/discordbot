import { randomInt } from 'node:crypto';

/**
 * 8桁のランダムな数字ワンタイムコードを生成する (10000000 〜 99999999)
 * @returns {string}
 */
export function generateLinkCode() {
  return String(randomInt(10000000, 100000000));
}

/**
 * 2つの文字列の編集距離（レーベンシュタイン距離）を計算する
 * タイポ・打ち間違い検知に使用
 * @param {string} a
 * @param {string} b
 * @returns {number}
 */
export function levenshteinDistance(a, b) {
  const m = a.length;
  const n = b.length;
  const dp = Array.from({ length: m + 1 }, () => new Array(n + 1).fill(0));

  for (let i = 0; i <= m; i++) dp[i][0] = i;
  for (let j = 0; j <= n; j++) dp[0][j] = j;

  for (let i = 1; i <= m; i++) {
    for (let j = 1; j <= n; j++) {
      if (a[i - 1] === b[j - 1]) {
        dp[i][j] = dp[i - 1][j - 1];
      } else {
        dp[i][j] = Math.min(
          dp[i - 1][j] + 1,     // 削除
          dp[i][j - 1] + 1,     // 挿入
          dp[i - 1][j - 1] + 1  // 置換
        );
      }
    }
  }

  return dp[m][n];
}
