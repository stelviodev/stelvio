package forwarder

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"net/netip"

	"github.com/stelviodev/traforo/internal/dnsrelay"
	"github.com/stelviodev/traforo/internal/protocol"
)

const controlLimit = 32768

type command struct {
	Version    int      `json:"version"`
	ID         uint64   `json:"id"`
	Operation  string   `json:"operation"`
	Unit       uint32   `json:"unit"`
	Generation uint64   `json:"generation"`
	Attempt    uint64   `json:"attempt"`
	Proxy      string   `json:"proxy,omitempty"`
	CIDRs      []string `json:"cidrs,omitempty"`
	Revision   uint64   `json:"revision,omitempty"`
	Resolver   string   `json:"resolver,omitempty"`
	Domains    []string `json:"domains,omitempty"`
	Hostnames  []string `json:"hostnames,omitempty"`
	Name       string   `json:"name,omitempty"`
	Port       uint16   `json:"port,omitempty"`
	Direct     bool     `json:"direct,omitempty"`
}
type reply struct {
	Diagnostics int    `json:"diagnostics,omitempty"`
	Version     int    `json:"version"`
	ID          uint64 `json:"id"`
	Status      string `json:"status"`
	Code        string `json:"code,omitempty"`
	Port        uint16 `json:"port,omitempty"`
	Observed    bool   `json:"observed"`
}
type dnsUnit struct {
	metrics                       *dnsrelay.Metrics
	generation, attempt, revision uint64
	relay                         *dnsrelay.Relay
	claims                        []string
}

func decodeCommand(wire []byte) (command, error) {
	var c command
	if len(wire) > controlLimit {
		return c, errors.New("command size")
	}
	d := json.NewDecoder(bytes.NewReader(wire))
	d.DisallowUnknownFields()
	err := d.Decode(&c)
	if err == nil {
		var extra any
		if d.Decode(&extra) != io.EOF {
			err = errors.New("trailing command")
		}
	}
	if err == nil && (c.Version != 2 || c.ID == 0 || c.Unit == 0 || c.Generation == 0 || c.Attempt == 0) {
		err = errors.New("command identity")
	}
	return c, err
}

func (m *manager) control(c command) reply {
	r := reply{Version: 2, ID: c.ID, Status: "ok"}
	var err error
	switch c.Operation {
	case "add", "remove":
		if c.Revision != 0 || c.Resolver != "" || len(c.Domains) != 0 || len(c.Hostnames) != 0 || c.Name != "" || c.Port != 0 || c.Direct {
			err = errors.New("unexpected DNS fields")
			break
		}
		if c.Operation == "add" {
			// Retained DNS state also fences unit generations and transport attempts.
			if d := m.dns[c.Unit]; d != nil {
				if c.Generation <= d.generation || c.Attempt <= d.attempt {
					err = dnsrelay.ErrStale
					break
				}
			}
		}
		if c.Operation == "remove" {
			if d := m.dns[c.Unit]; d != nil && d.relay != nil && d.generation == c.Generation && d.attempt == c.Attempt {
				err = d.relay.Reject(d.revision)
				if err != nil {
					break
				}
			}
		}
		err = m.apply(c)
	case "dns-configure":
		if c.Name != "" || c.Port != 0 || c.Direct {
			err = errors.New("unexpected probe fields")
			break
		}
		u := m.units[c.Unit]
		if u == nil || u.generation != c.Generation || m.generations[c.Unit] != c.Attempt {
			err = dnsrelay.ErrStale
			break
		}
		if m.dns == nil {
			m.dns = make(map[uint32]*dnsUnit)
		}
		resolver, e := netip.ParseAddr(c.Resolver)
		if e != nil {
			err = e
			break
		}
		// Proxy and selected destination ranges come from the validated packet unit.
		// A DNS update cannot add or broaden resource destinations.
		if c.Proxy != "" || len(c.CIDRs) != 0 {
			err = errors.New("unexpected network fields")
			break
		}
		config := dnsrelay.Config{Revision: c.Revision, Domains: c.Domains, Hostnames: c.Hostnames, Resolver: resolver, Proxy: u.proxy, CIDRs: u.cidrs}
		if err = config.Validate(); err != nil {
			break
		}
		claims := append(append([]string{}, c.Domains...), c.Hostnames...)
		for id, d := range m.dns {
			if id != c.Unit {
				for _, a := range claims {
					for _, b := range d.claims {
						if protocol.DNSOverlap(a, b) {
							err = errors.New("conflicting DNS ownership")
						}
					}
				}
			}
		}
		if err != nil {
			break
		}
		d := m.dns[c.Unit]
		if d != nil {
			if c.Generation < d.generation || c.Attempt < d.attempt || c.Revision <= d.revision {
				err = dnsrelay.ErrStale
				break
			}
			if c.Generation == d.generation {
				for _, old := range d.claims {
					found := false
					for _, next := range claims {
						found = found || old == next
					}
					if !found {
						err = errors.New("DNS ownership cannot shrink")
					}
				}
			}
		}
		if err != nil {
			break
		}
		if d == nil || d.relay == nil {
			// Keep at most eight identities, including retirement high-water marks.
			if d == nil && len(m.dns) >= protocol.MaxUnits {
				err = errors.New("DNS unit bound")
				break
			}
			var relay *dnsrelay.Relay
			if d == nil {
				d = &dnsUnit{generation: c.Generation}
			}
			if d.metrics == nil {
				d.metrics = &dnsrelay.Metrics{}
			}
			relay, err = dnsrelay.NewWithMetrics(config, d.metrics)
			if err == nil {
				m.dns[c.Unit] = d
				d.relay = relay
			}
		} else {
			err = d.relay.Activate(config)
		}
		if err == nil {
			d.generation = c.Generation
			d.attempt = c.Attempt
			d.revision = c.Revision
			d.claims = claims
			r.Port = d.relay.Port()
		}
	case "dns-reject", "dns-retire", "dns-probe", "dns-arm", "dns-check", "dns-release":
		if c.Proxy != "" || len(c.CIDRs) != 0 || c.Resolver != "" || len(c.Domains) != 0 || len(c.Hostnames) != 0 {
			err = errors.New("unexpected configuration fields")
			break
		}
		d := m.dns[c.Unit]
		if d == nil || d.relay == nil || d.generation != c.Generation || d.attempt != c.Attempt || d.revision != c.Revision {
			err = dnsrelay.ErrStale
			break
		}
		switch c.Operation {
		case "dns-reject", "dns-retire":
			if c.Name != "" || c.Port != 0 || c.Direct {
				err = errors.New("unexpected DNS fields")
				break
			}
			err = d.relay.Reject(c.Revision)
			if err == nil && c.Operation == "dns-retire" {
				d.relay.Close()
				d.relay = nil
				d.claims = nil // Retain generation/attempt/revision fencing after closing sockets.
			}
		case "dns-probe":
			err = d.relay.Probe(c.Revision, c.Name, c.Port, c.Direct)
		default:
			if c.Port != 0 || c.Direct {
				err = errors.New("unexpected probe fields")
				break
			}
			r.Observed, err = d.relay.Observe(c.Operation[len("dns-"):], c.Name, c.Revision)
		}
	default:
		err = errors.New("unknown operation")
	}
	if err != nil {
		r.Status = "error"
		r.Code = "invalid"
		if errors.Is(err, dnsrelay.ErrStale) {
			r.Code = "stale"
		} else if errors.Is(err, dnsrelay.ErrAuthorization) {
			r.Code = "authorization"
		} else if c.Operation == "dns-probe" {
			r.Code = "unavailable"
		}
	}
	return r
}
func (m *manager) closeDNS() {
	for _, d := range m.dns {
		if d.relay != nil {
			d.relay.Close()
		}
	}
}

func (m *manager) serveControl(input io.Reader, output io.Writer) error {
	encoder := json.NewEncoder(output)
	if err := encoder.Encode(reply{Version: 2, Status: "ready", Diagnostics: m.diagnosticsVersion()}); err != nil {
		return err
	}
	scanner := bufio.NewScanner(input)
	scanner.Buffer(make([]byte, 4096), controlLimit+1)
	var lastID uint64
	for scanner.Scan() {
		c, err := decodeCommand(scanner.Bytes())
		r := reply{Version: 2, ID: c.ID, Status: "error", Code: "invalid"}
		if err == nil && c.ID > lastID {
			lastID = c.ID
			r = m.control(c)
			m.refreshDiagnostics()
		}
		if err = encoder.Encode(r); err != nil {
			return err
		}
	}
	return scanner.Err()
}
