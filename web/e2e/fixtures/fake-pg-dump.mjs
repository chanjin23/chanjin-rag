// E2E 전용 가짜 `pg_dump`(BACKUP_PG_DUMP_CMD). 실제 DB를 덤프하지 않고 `-f <파일>`에 합성 바이트를 쓴다.
// 백엔드는 `pg_dump -Fc -f <파일> --dbname=<주소>`로 부르고, 비밀번호는 PGPASSWORD로 넘긴다.
import { writeFileSync } from "node:fs";

const args = process.argv.slice(2);
const out = args[args.indexOf("-f") + 1];
if (!args.includes("-Fc") || !out) {
  console.error("fake pg_dump: -Fc -f <file> required");
  process.exit(2);
}
writeFileSync(out, `PGDMP e2e fake dump ${new Date().toISOString()}\n`);
