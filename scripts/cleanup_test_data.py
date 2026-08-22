import pymysql

c = pymysql.connect(host="192.168.100.150", port=23306, user="ai_bot",
                    password="e484PP6GRwzZGFkX", database="ai_bot")
cur = c.cursor()
cur.execute("DELETE FROM notifications")
cur.execute("SELECT job_key, cron FROM scheduled_jobs "
            "WHERE job_key LIKE '%briefing%' OR job_key='evening_review'")
print("briefing crons:", cur.fetchall())
c.commit()
c.close()
