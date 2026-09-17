local function default_port()
  return 8080
end

local function endpoint(host)
  return host .. ":" .. default_port()
end

return { endpoint = endpoint }
