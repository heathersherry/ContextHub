from integrations.hydrabench.run import public_input


def test_only_question_and_database_reach_agent():
    case = {"id": "dev_test", "db_id": "toy", "final_question": "What is the value?",
            "sql_query": "SECRET_SQL", "final_answer": "SECRET_ANSWER", "type": "s2a",
            "key_term": "SECRET_ENTITY", "evidence": "SECRET_EVIDENCE",
            "hint": {"high": "SECRET_REASONING"}, "decomposition": ["SECRET_STEP"]}
    assert public_input(case) == "Database: toy\nQuestion: What is the value?"
