import psycopg2

DB_CONFIG = {
    "dbname": "mlops_db",
    "user": "postgres",
    "password": "password",
    "host": "localhost",
    "port": "5432",
}

conn = psycopg2.connect(**DB_CONFIG)
cursor = conn.cursor()

print("=== 데이터 품질 분석 ===\n")

# 1. 카테고리 분포
print("1. 에러 카테고리 분포:")
cursor.execute(
    "SELECT error_category, COUNT(*) FROM agent_error_playbook GROUP BY error_category ORDER BY COUNT(*) DESC;"
)
for cat, count in cursor.fetchall():
    print(f"   {cat}: {count}건")

# 2. 심각도 분포
print("\n2. 심각도 분포:")
cursor.execute(
    "SELECT severity, COUNT(*) FROM agent_error_playbook GROUP BY severity ORDER BY COUNT(*) DESC;"
)
for sev, count in cursor.fetchall():
    print(f"   {sev}: {count}건")

# 3. 조치 유형 분포
print("\n3. 조치 유형(action_type) 분포:")
cursor.execute(
    "SELECT action_type, COUNT(*) FROM agent_error_playbook GROUP BY action_type ORDER BY COUNT(*) DESC;"
)
for action, count in cursor.fetchall():
    print(f"   {action}: {count}건")

# 4. NULL 또는 빈 값 확인
print("\n4. 데이터 품질 검사:")
cursor.execute(
    "SELECT COUNT(*) FROM agent_error_playbook WHERE log_text IS NULL OR log_text = '';"
)
null_logs = cursor.fetchone()[0]
print(f"   빈 log_text: {null_logs}건")

cursor.execute(
    "SELECT COUNT(*) FROM agent_error_playbook WHERE error_category IS NULL OR error_category = '';"
)
null_cats = cursor.fetchone()[0]
print(f"   빈 error_category: {null_cats}건")

# 5. 매우 짧은 데이터 확인
print("\n5. 데이터 길이 분석:")
cursor.execute("SELECT COUNT(*) FROM agent_error_playbook WHERE LENGTH(log_text) < 50;")
short_logs = cursor.fetchone()[0]
print(f"   50글자 미만: {short_logs}건")

cursor.execute(
    "SELECT COUNT(*) FROM agent_error_playbook WHERE LENGTH(log_text) > 5000;"
)
long_logs = cursor.fetchone()[0]
print(f"   5000글자 초과: {long_logs}건")

# 6. 50글자 미만인 것들 일부 확인
if short_logs > 0:
    print("\n6. 50글자 미만 데이터 샘플:")
    cursor.execute(
        "SELECT error_category, severity, log_text FROM agent_error_playbook WHERE LENGTH(log_text) < 50 LIMIT 10;"
    )
    for cat, sev, log in cursor.fetchall():
        print(f"   [{sev}] {cat} - {log[:60]}")

# 7. 평균 길이
print("\n7. log_text 길이 통계:")
cursor.execute(
    "SELECT MIN(LENGTH(log_text)), AVG(LENGTH(log_text)), MAX(LENGTH(log_text)) FROM agent_error_playbook;"
)
min_len, avg_len, max_len = cursor.fetchone()
print(f"   최소: {min_len}글자")
print(f"   평균: {avg_len:.0f}글자")
print(f"   최대: {max_len}글자")

# 8. 중복 체크
print("\n8. 중복 체크:")
cursor.execute("SELECT COUNT(*), COUNT(DISTINCT log_text) FROM agent_error_playbook;")
total, distinct = cursor.fetchone()
duplicates = total - distinct
print(f"   전체: {total}건")
print(f"   중복 제외: {distinct}건")
print(f"   중복: {duplicates}건")

cursor.close()
conn.close()
