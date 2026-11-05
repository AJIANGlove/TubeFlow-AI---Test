--  第 40 单元的文案（判断是否「一时一地只描述同一物件」）与提示词版本
select u.ordinal, u.prompt_ref, jsonb_array_length(u.sentences_json::jsonb) as sentence_count,
       u.sentences_json
from project_director_units u
where u.run_id = (select id from project_director_runs where id::text like 'be6678d5%')
  and u.ordinal = 40;
