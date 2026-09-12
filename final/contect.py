from sqlalchemy import create_engine, text

# '원격서버IP주소' 부분에 실제 서버의 IP나 도메인을 입력하세요.
DB_URL = "mysql+pymysql://admin:qwer1234@team2.cbwq8acackv5.ap-northeast-2.rds.amazonaws.com/team2"

try:
    engine = create_engine(DB_URL)

    with engine.connect() as connection:
        result = connection.execute(text("SELECT VERSION();"))
        db_version = result.fetchone()[0]

        print("🎉 원격 MySQL 데이터베이스 연결 성공!")
        print(f"MySQL 버전: {db_version}")

except Exception as e:
    print("❌ 데이터베이스 연결 실패...")
    print(e)