package main

import (
	"bytes"
	"encoding/csv"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	"golang.org/x/text/encoding/simplifiedchinese"
)

type csvMapping struct{ Date, Direction, Amount, Category, Description, Note int }
type importPreview struct {
	UserID   int64
	Raw      []byte
	Headers  []string
	Mapping  csvMapping
	Rows     []transaction
	Problems []string
	Created  time.Time
}
type previewStore struct {
	mu    sync.Mutex
	items map[string]*importPreview
}
type previewData struct {
	ID       string
	Headers  []string
	Mapping  csvMapping
	Rows     []transaction
	Problems []string
	Total    int
}

func newPreviewStore() *previewStore { return &previewStore{items: map[string]*importPreview{}} }
func (s *previewStore) put(id string, p *importPreview) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for k, v := range s.items {
		if time.Since(v.Created) > 15*time.Minute {
			delete(s.items, k)
		}
	}
	if len(s.items) >= 20 {
		for k := range s.items {
			delete(s.items, k)
			break
		}
	}
	s.items[id] = p
}
func (s *previewStore) get(id string, userID int64) (*importPreview, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	p, ok := s.items[id]
	if !ok || p.UserID != userID || time.Since(p.Created) > 15*time.Minute {
		return nil, false
	}
	return p, true
}
func (s *previewStore) drop(id string) { s.mu.Lock(); defer s.mu.Unlock(); delete(s.items, id) }

func decodeCSV(raw []byte) ([]byte, error) {
	raw = bytes.TrimPrefix(raw, []byte{0xef, 0xbb, 0xbf})
	if utf8.Valid(raw) {
		return raw, nil
	}
	decoded, err := simplifiedchinese.GB18030.NewDecoder().Bytes(raw)
	if err != nil {
		return nil, errors.New("文件编码无法识别，请使用 UTF-8 或 GB18030 CSV")
	}
	return decoded, nil
}

func csvRecords(raw []byte) ([][]string, error) {
	decoded, err := decodeCSV(raw)
	if err != nil {
		return nil, err
	}
	reader := csv.NewReader(bytes.NewReader(decoded))
	reader.FieldsPerRecord = -1
	reader.LazyQuotes = true
	reader.TrimLeadingSpace = true
	rows := [][]string{}
	for {
		row, err := reader.Read()
		if err == io.EOF {
			break
		}
		if err != nil {
			return nil, err
		}
		if len(rows) > 5000 {
			return nil, errors.New("单次最多导入 5000 行")
		}
		rows = append(rows, row)
	}
	if len(rows) < 2 {
		return nil, errors.New("CSV 至少需要标题行和一条记录")
	}
	return rows, nil
}

func matchHeader(headers []string, aliases ...string) int {
	for i, h := range headers {
		h = strings.ToLower(strings.TrimSpace(h))
		for _, a := range aliases {
			if h == strings.ToLower(a) {
				return i
			}
		}
	}
	for i, h := range headers {
		h = strings.ToLower(strings.TrimSpace(h))
		for _, a := range aliases {
			if strings.Contains(h, strings.ToLower(a)) {
				return i
			}
		}
	}
	return -1
}

func autoMapping(headers []string) csvMapping {
	return csvMapping{
		Date: matchHeader(headers, "日期", "交易时间", "时间", "date"), Direction: matchHeader(headers, "收支", "收/支", "交易类型", "direction", "type"), Amount: matchHeader(headers, "金额", "交易金额", "amount"), Category: matchHeader(headers, "分类", "类别", "category"), Description: matchHeader(headers, "交易说明", "商品", "摘要", "对方", "description", "备注"), Note: matchHeader(headers, "备注", "note"),
	}
}

func cell(row []string, i int) string {
	if i >= 0 && i < len(row) {
		return strings.TrimSpace(row[i])
	}
	return ""
}
func parseCSVDate(raw string) string {
	for _, layout := range []string{"2006-01-02", "2006/01/02", "2006/1/2", "2006-1-2", "2006-01-02 15:04:05", "2006/01/02 15:04:05", "2006-01-02T15:04:05"} {
		if t, err := time.Parse(layout, raw); err == nil {
			return t.Format("2006-01-02")
		}
	}
	if len(raw) >= 10 && validDate(raw[:10]) {
		return raw[:10]
	}
	return ""
}

func classifyDescription(desc string) string {
	for _, rule := range []struct {
		words    []string
		category string
	}{
		{[]string{"工资", "薪资", "salary"}, "工资"}, {[]string{"房租", "房贷", "物业"}, "住房"}, {[]string{"餐", "饭", "外卖", "超市"}, "餐饮"}, {[]string{"地铁", "公交", "打车", "加油"}, "交通"}, {[]string{"医疗", "医院", "药"}, "医疗"}, {[]string{"学费", "课程", "教育"}, "教育"}, {[]string{"还款", "信用卡"}, "还款"}, {[]string{"电影", "游戏", "娱乐"}, "娱乐"}, {[]string{"购物", "商场"}, "购物"},
	} {
		for _, word := range rule.words {
			if strings.Contains(strings.ToLower(desc), word) {
				return rule.category
			}
		}
	}
	return "其他"
}

func parseCSVTransactions(raw []byte, m csvMapping) ([]transaction, []string, error) {
	records, err := csvRecords(raw)
	if err != nil {
		return nil, nil, err
	}
	if m.Date < 0 || m.Amount < 0 {
		return nil, nil, errors.New("请指定日期和金额列")
	}
	items := []transaction{}
	problems := []string{}
	for idx, row := range records[1:] {
		date := parseCSVDate(cell(row, m.Date))
		amountRaw := cell(row, m.Amount)
		negative := strings.HasPrefix(strings.TrimSpace(amountRaw), "-")
		amountRaw = strings.TrimPrefix(strings.TrimSpace(amountRaw), "-")
		amount, e := parseMoney(amountRaw)
		if date == "" || e != nil || amount <= 0 {
			if len(problems) < 20 {
				problems = append(problems, fmt.Sprintf("第 %d 行日期或金额无效", idx+2))
			}
			continue
		}
		directionRaw := strings.ToLower(cell(row, m.Direction))
		direction := "expense"
		if strings.Contains(directionRaw, "收") || strings.Contains(directionRaw, "入") || directionRaw == "income" || directionRaw == "+" {
			direction = "income"
		}
		if negative {
			direction = "expense"
		}
		desc := cell(row, m.Description)
		category := cell(row, m.Category)
		if !validCategory(category) {
			category = classifyDescription(desc)
		}
		if m.Direction < 0 && category == "工资" {
			direction = "income"
		}
		if len(desc) > 200 {
			desc = desc[:200]
		}
		note := cell(row, m.Note)
		if len(note) > 500 {
			note = note[:500]
		}
		items = append(items, transaction{Date: date, Direction: direction, AmountCents: amount, Category: category, Description: desc, Note: note})
	}
	return items, problems, nil
}

func (a *app) importCSV(w http.ResponseWriter, r *http.Request) {
	r.Body = http.MaxBytesReader(w, r.Body, 2<<20)
	if err := r.ParseMultipartForm(2 << 20); err != nil {
		fail(w, r, "/ledger", "CSV 文件不能超过 2 MiB")
		return
	}
	file, _, err := r.FormFile("file")
	if err != nil {
		fail(w, r, "/ledger", "请选择 CSV 文件")
		return
	}
	defer file.Close()
	raw, err := io.ReadAll(io.LimitReader(file, (2<<20)+1))
	if err != nil || len(raw) > 2<<20 {
		fail(w, r, "/ledger", "CSV 文件读取失败或过大")
		return
	}
	records, err := csvRecords(raw)
	if err != nil {
		fail(w, r, "/ledger", err.Error())
		return
	}
	id, err := randomHex(16)
	if err != nil {
		http.Error(w, "预览失败", 500)
		return
	}
	p := &importPreview{UserID: currentUser(r).ID, Raw: raw, Headers: records[0], Mapping: autoMapping(records[0]), Created: time.Now()}
	p.Rows, p.Problems, _ = parseCSVTransactions(raw, p.Mapping)
	a.previews.put(id, p)
	redirect(w, r, "/ledger/preview?id="+id, "")
}

func (a *app) previewCSV(w http.ResponseWriter, r *http.Request) {
	id := r.URL.Query().Get("id")
	if id == "" {
		id = r.FormValue("id")
	}
	p, ok := a.previews.get(id, currentUser(r).ID)
	if !ok {
		fail(w, r, "/ledger", "预览已过期，请重新上传")
		return
	}
	if r.Method == http.MethodPost {
		parseIndex := func(key string) int {
			n, err := strconv.Atoi(r.FormValue(key))
			if err != nil || n < -1 || n >= len(p.Headers) {
				return -1
			}
			return n
		}
		p.Mapping = csvMapping{Date: parseIndex("date"), Direction: parseIndex("direction"), Amount: parseIndex("amount"), Category: parseIndex("category"), Description: parseIndex("description"), Note: parseIndex("note")}
		p.Rows, p.Problems, _ = parseCSVTransactions(p.Raw, p.Mapping)
	}
	visible := p.Rows
	if len(visible) > 20 {
		visible = visible[:20]
	}
	a.render(w, r, "preview.html", pageData{Title: "确认 CSV 导入", Active: "ledger", Payload: previewData{ID: id, Headers: p.Headers, Mapping: p.Mapping, Rows: visible, Problems: p.Problems, Total: len(p.Rows)}})
}

func (a *app) confirmCSV(w http.ResponseWriter, r *http.Request) {
	id := r.FormValue("id")
	p, ok := a.previews.get(id, currentUser(r).ID)
	if !ok {
		fail(w, r, "/ledger", "预览已过期，请重新上传")
		return
	}
	if p.Mapping.Date < 0 || p.Mapping.Amount < 0 || len(p.Rows) == 0 {
		fail(w, r, "/ledger/preview?id="+id, "请先确认日期和金额列")
		return
	}
	inserted, duplicates, err := a.insertImported(currentUser(r).ID, p.Rows)
	if err != nil {
		if errors.Is(err, errLedgerCapacity) {
			fail(w, r, "/ledger", "导入后会超过当前账号账本容量上限")
			return
		}
		http.Error(w, "导入失败", 500)
		return
	}
	a.previews.drop(id)
	if inserted > 0 {
		if err := a.setLedgerStatus(currentUser(r).ID, "imported"); err != nil {
			http.Error(w, "流程保存失败", 500)
			return
		}
	}
	redirect(w, r, "/ledger", fmt.Sprintf("已导入 %d 条，跳过重复 %d 条", inserted, duplicates))
}
