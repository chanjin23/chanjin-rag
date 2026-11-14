// E2E 전용 가짜 `age`(BACKUP_AGE_CMD). `age -r <공개키> -o <출력> <입력>`을 흉내 내 헤더를 붙여 쓴다.
// 실제 암호화는 하지 않는다(E2E는 파이프라인 연결과 파일 전달만 확인).
import { readFileSync, writeFileSync } from "node:fs";

const args = process.argv.slice(2);
const recipient = args[args.indexOf("-r") + 1];
const out = args[args.indexOf("-o") + 1];
const input = args[args.length - 1];
if (!recipient || !out || !input) {
  console.error("fake age: -r <recipient> -o <out> <in> required");
  process.exit(2);
}
const body = readFileSync(input);
writeFileSync(out, Buffer.concat([Buffer.from("age-encryption.org/v1 (e2e fake)\n"), body]));
