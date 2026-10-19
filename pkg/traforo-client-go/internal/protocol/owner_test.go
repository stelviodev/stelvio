package protocol

import "testing"

func TestDarwinOwnerIdentityValidation(t *testing.T) {
	valid := DarwinOwner{
		UID:     501,
		PID:     123,
		Seconds: 10,
		Micros:  999999,
		Token:   [8]uint32{0, 501, 0, 501, 0, 123},
	}
	if !valid.ValidV3() {
		t.Fatal("rejected valid Darwin owner identity")
	}

	invalid := []DarwinOwner{valid, valid, valid, valid, valid, valid}
	invalid[0].UID = 0
	invalid[1].PID = 0
	invalid[2].Seconds = 0
	invalid[3].Micros = 1000000
	invalid[4].Token[1]++
	invalid[5].Token[5]++
	for i, candidate := range invalid {
		if candidate.ValidV3() {
			t.Errorf("accepted invalid owner identity %d", i)
		}
	}
}

func TestDarwinParentIdentityValidation(t *testing.T) {
	valid := DarwinParent{UID: 501, PID: 123, Seconds: 10, Micros: 999999}
	if !valid.Valid() {
		t.Fatal("rejected valid Darwin parent identity")
	}

	invalid := []DarwinParent{valid, valid, valid, valid, valid}
	invalid[0].UID = 0
	invalid[1].PID = 1
	invalid[2].Seconds = 0
	invalid[3].Micros = 1000000
	invalid[4].PID = 0
	for i, candidate := range invalid {
		if candidate.Valid() {
			t.Errorf("accepted invalid parent identity %d", i)
		}
	}
}
