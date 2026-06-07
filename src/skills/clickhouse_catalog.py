# Open: C:\Users\ls3412\Desktop\A2A\src\skills\clickhouse_catalog.py
import clickhouse_connect
from config.settings import get_settings

class ClickHouseCatalog:
    def __init__(self):
        # Dynamically load config values from .env
        self.settings = get_settings()
        self.client = clickhouse_connect.get_client(
            host=self.settings.clickhouse.host,
            port=self.settings.clickhouse.port,
            username=self.settings.clickhouse.user,
            password=self.settings.clickhouse.password # <-- Now correctly loads 'secure_password_change_me'
        )

    def get_relevant_tables(self, user_question: str, limit: int = 4) -> list[str]:
        keywords = [word.lower() for word in user_question.split() if len(word) > 3]
        if not keywords:
            return ["stores", "active_tasks"]
            
        term_conditions = " OR ".join([f"hasToken(lower(description), '{kw}') OR hasToken(lower(table_name), '{kw}')" for kw in keywords])
        query = f"""
            SELECT DISTINCT table_name
            FROM table_metadata_catalog
            WHERE db_source = 'postgres' AND ({term_conditions})
            LIMIT {limit}
        """
        try:
            result = self.client.query(query)
            tables = [row[0] for row in result.result_rows]
            return tables if tables else ["stores", "active_tasks"]
        except Exception:
            return ["stores", "active_tasks"]
