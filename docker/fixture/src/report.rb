def format_money(cents)
  "%.2f" % (cents / 100.0)
end

def render_line(label, cents)
  "#{label}: #{format_money(cents)}"
end
